import numpy

from graph import Operator, InputSlot, OutputSlot, MultiInputSlot, MultiOutputSlot
from roi import sliceToRoi, roiToSlice, block_view
from Queue import Empty
from collections import deque
import greenlet, threading
import vigra

class OpArrayPiper(Operator):
    inputSlots = [InputSlot("Input")]
    outputSlots = [OutputSlot("Output")]    
    
    def notifyConnect(self, inputSlot):
        self.outputs["Output"]._dtype = inputSlot.dtype
        self.outputs["Output"]._shape = inputSlot.shape
        self.outputs["Output"]._axistags = inputSlot.axistags

    @property
    def shape(self):
        return self.outputs["Output"]._shape
    
    @property
    def dtype(self):
        return self.outputs["Output"]._dtype        


import drtile

class BlockQueue(object):
    __slots__ = ["queue","lock"]
    
    def __init__(self):
        self.queue = None
        self.lock = threading.Lock()
              
class OpArrayCache(OpArrayPiper):
    def __init__(self, graph, blockShape = None, immediateAlloc = True):
        OpArrayPiper.__init__(self, graph)
        if blockShape == None:
            blockShape = 128
        self._blockShape = blockShape
        self._immediateAlloc = immediateAlloc

    def notifyConnect(self, inputSlot):
        OpArrayPiper.notifyConnect(self, inputSlot)
        self._cache = numpy.ndarray(self.shape, dtype = self.dtype)
        
        if type(self._blockShape) != tuple:
            self._blockShape = (self._blockShape,)*len(self.shape)
            
        self._dirtyShape = numpy.ceil(1.0 * numpy.array(self.shape) / numpy.array(self._blockShape))
        # if the entry in _dirtyArray differs from _dirtyState
        # the entry is considered dirty
        self._blockQuery = numpy.ndarray(self._dirtyShape, dtype=object)
        self._blockState = numpy.ones(self._dirtyShape, numpy.uint8)

        _blockNumbers = numpy.dstack(numpy.nonzero(self._blockState.ravel()))
        _blockNumbers.shape = self._dirtyShape

        _blockIndices = numpy.dstack(numpy.nonzero(self._blockState))
        _blockIndices.shape = self._blockState.shape + (_blockIndices.shape[-1],)

         
        self._blockNumbers = _blockNumbers
        self._blockIndices = _blockIndices
        
        self._blockState[:]= 1
        self._dirtyState = 2
        
        self._lock = threading.Lock()
        
        # allocate queryArray object
        self._flatBlockIndices =  self._blockIndices[:]
        self._flatBlockIndices = self._flatBlockIndices.reshape(self._flatBlockIndices.size/self._flatBlockIndices.shape[-1],self._flatBlockIndices.shape[-1],)
#        for p in self._flatBlockIndices:
#            self._blockQuery[p] = BlockQueue()
            

    def setDirty(self, inputSlot=None):
        self._lock.acquire()
#        self._dirtyState = 2
#        self._blockState[:] = 1
        self._dirtyState += 1
        self._lock.release()
        OpArrayPiper.setDirty(self, inputSlot=inputSlot)
        print "OpArrayCache setDirty"
        
    def getOutSlot(self,slot,key,result):
        start, stop = sliceToRoi(key, self.shape)
        
#        print "Request::::: ", key 
        self._lock.acquire()
        blockStart = numpy.floor(1.0 * start / self._blockShape)
        blockStop = numpy.ceil(1.0 * stop / self._blockShape)
        blockKey = roiToSlice(blockStart,blockStop)
                
        #blockInd =  self._blockNumbers[blockKey].ravel()
        #blockInd = blockInd.reshape(blockInd.size/blockInd.shape[-1],blockInd.shape[-1],)
        #dirtyBlockIndicator = numpy.where(self._blockState[blockKey] != self._dirtyState and self._blockState[blockKey] != 0, 0, 1)        
        
        inProcessIndicator = numpy.where(self._blockState[blockKey] == 0, 1, 0)
        
        inProcessQueries = numpy.unique(numpy.extract(inProcessIndicator == 1, self._blockQuery[blockKey]))

        # calculate the blockIndices of dirty elements
        cond = (self._blockState[blockKey] != self._dirtyState) * (self._blockState[blockKey] != 0)
        #dirtyBlockNums = numpy.extract(cond.ravel(), self._blockNumbers[blockKey].ravel())
        #dirtyBlockInd = self._flatBlockIndices[dirtyBlockNums,:]
        tileWeights = numpy.where(cond, 1, 256**3+1)       
        trueDirtyIndices = numpy.nonzero(numpy.where(cond, 1,0))
        
        tileWeights = vigra.ScalarVolume(tileWeights, dtype = numpy.uint32)
        
#        print "calling drtile..."
        tileArray = drtile.test_DRTILE(tileWeights, 256**3 + 1)
#        print "finished calling drtile."
        dirtyRois = []
        half = tileArray.shape[0]/2
        dirtyRequests = []
#        print "Original Key %r, split into %d requests" % (key, tileArray.shape[1])
#        print self._blockState[blockKey][trueDirtyIndices]
#        print "Ranges:"
#        print "TileArray:", tileArray
        for i in range(tileArray.shape[1]):

            #drStart2 = (tileArray[half-1::-1,i] + blockStart)
            #drStop2 = (tileArray[half*2:half-1:-1,i] + blockStart)
            drStart2 = (tileArray[:half,i] + blockStart)
            drStop2 = (tileArray[half:,i] + blockStart)
            drStart = drStart2*self._blockShape
            drStop = drStop2*self._blockShape
            drStop = numpy.minimum(drStop, self.shape)
            dirtyRois.append([drStart,drStop])
        
            #set up a new block query object
            bq = BlockQueue()
            bq.queue = deque()
            key = roiToSlice(drStart,drStop)
            key2 = roiToSlice(drStart2,drStop2)
#            print "Request %d: %r" %(i,key)

            
            self._blockQuery[key2] = bq
            if (self._blockState[key2] == self._dirtyState).any() or (self._blockState[key2] == 0).any():
                import h5py
                f = h5py.File("test.h5", "w")
                f.create_dataset("data",data = tileWeights)
                print "%r \n %r \n %r\n %r\n %r \n%r" % (key2, blockKey,self._blockState[key2], self._blockState[blockKey][trueDirtyIndices],self._blockState[blockKey],tileWeights)
                assert 1 == 2
            
#            assert(self._blockState[key2] != 0).all(), "%r, %r, %r, %r, %r,%r" % (key2, blockKey, self._blockState[key2], self._blockState[blockKey][trueDirtyIndices],self._blockState[blockKey], tileWeights)
            dirtyRequests.append((bq,key,drStart,drStop))
            
#        # indicate the inprocessing state, by setting array to 0        
        self._blockState[blockKey] = numpy.where(cond, 0, self._blockState[blockKey])
                
        self._lock.release()
        
        requests = []
        #fire off requests
        for r in dirtyRequests:
            bq, key, reqStart, reqStop = r
            req = self.inputs["Input"][key].writeInto(self._cache[key])
            requests.append(req)
            
        #print "requests fired"
        
        
#        if len(requests)>0:
#            print "number of fired requests:", len(requests)
        #wait for all requests to finish
        for req in requests:
            req()

        #print "requests finished"

        # indicate the finished inprocess state        
        self._lock.acquire()
        self._blockState[blockKey] = numpy.where(cond, self._dirtyState, self._blockState[blockKey])
        self._lock.release()

        
        #notify eventual waiters
        for r in dirtyRequests:
            bq, key, reqStart, reqStop = r
            bq.lock.acquire()
            for w in bq.queue:
                #[None, gr,event,thread]
                w[3].pendingGreenlets.append(w)
            bq.queue = None
            bq.lock.release()
        
        
        #wait for all in process queries
        for q in inProcessQueries:
            q.lock.acquire()
            if q.queue is not None:
                temp = numpy.ndarray((1,), dtype = object)
                temp[0] = greenlet.getcurrent()
                task = [None, temp, threading.Event(),threading.current_thread()]
                q.queue.append(task)
                q.lock.release()
                greenlet.getcurrent().parent.switch(None)
            else:
                q.lock.release()
        
        
        # finally, store results in result area
        result[:] = self._cache[roiToSlice(start, stop)]
        
        
        
    def setInSlot(self, slot, key, value):
        start, stop = sliceToRoi(key, self.shape)
        blockStart = numpy.ceil(1.0 * start / self._blockShape)
        blockStop = numpy.floor(1.0 * stop / self._blockShape)
        blockStop = numpy.where(stop == self.shape, self._dirtyShape, blockStop)
        blockKey = roiToSlice(blockStart,blockStop)

        if (blockStop >= blockStart).all():
            start2 = blockStart * self._blockShape
            stop2 = blockStop * self._blockShape
            stop2 = numpy.minimum(stop2, self.shape)
            key2 = roiToSlice(start2,stop2)
            self._lock.acquire()
            self._cache[key2] = value[roiToSlice(start2-start,stop2-start)]
            self._blockState[blockKey] = self._dirtyState
            self._lock.release()
        
        #pass request on
        self.outputs["Output"][key] = value
        