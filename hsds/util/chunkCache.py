##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
import numpy
import hsds_logger as log

def getArraySize(arr):
    """ Return size in bytes of numpy array """
    nbytes = arr.dtype.itemsize
    for n in arr.shape:
        nbytes *= n
    return nbytes


class ChunkNode(object):
    def __init__(self, id, nparr, isdirty=False, prev=None, next=None):
        self._id = id
        self._nparr = nparr
        self._isdirty = isdirty
        self._prev = prev
        self._next = next
        self._mem_size = getArraySize(nparr)


class ChunkCache(object):
    """ LRU cache for Numpy arrays that are read/written from S3
    """
    def __init__(self, mem_target=32*1024*1024):
        self._hash = {}
        self._lru_head = None
        self._lru_tail = None
        self._mem_size = 0
        self._mem_target = mem_target
        self._dirty_set = set()

    def _getNode(self, chunk_id):
        """ Return node  """
        if chunk_id not in self._hash:
            raise KeyError(chunk_id)
        return self._hash[chunk_id]

    def _delNode(self, chunk_id):
        # remove from LRU
        node = self._getNode(chunk_id)
        prev = node._prev
        next = node._next
        if prev is None:
            if self._lru_head != node:
                raise KeyError("unexpected error")
            self._lru_head = next
        else:
            prev._next = next
        if next is None:
            if self._lru_tail != node:
                raise KeyError("unexpected error")
            self._lru_tail = prev
        else:
            next._prev = prev
        node._next = node._prev = None
        log.info("chunk {} removed from chunk cache".format(node._id))
        return node

    def _moveToFront(self, chunk_id):
        # move this node to the front of LRU list
        node = self._getNode(chunk_id)
        if self._lru_head == node:
            # already the front
            return node
        if node._prev is None:
            raise KeyError("unexpected error")
        prev = node._prev
        next = node._next
        node._prev = None
        node._next = self._lru_head
        prev._next = next
        self._lru_head._prev = node
        if next is not None:
            next._prev = prev
        else:
            if self._lru_tail != node:
                raise KeyError("unexpected error")
            self._lru_tail = prev
        self._lru_head = node
        log.info("new chunkcache headnode: {}".format(node._id))
        return node

    def __delitem__(self, chunk_id):
        node = self._delNode(chunk_id) # remove from LRU
        del self._hash[chunk_id]       # remove from hash
        # remove from LRU list
        
        self._mem_size -= node._mem_size
        if chunk_id in self._dirty_set:
            log.warning("removing dirty chunk from cache: {}".format(chunk_id))
            self._dirty_set.remove(chunk_id)

    def __len__(self):
        """ Number of chunks in the cache """
        return len(self._hash)

    def __iter__(self):
        """ Iterate over chunk ids """
        node = self._lru_head
        while node is not None:
            yield node._id
            node = node._next  

    def __contains__(self, chunk_id):
        """ Test if a chunk_id is in the cache """
        if chunk_id in self._hash:
            return True
        else:
            return False

    def __getitem__(self, chunk_id):
        """ Return numpy array from cache """
        # doing a getitem has the side effect of moving this node 
        # up in the LRU list
        node = self._moveToFront(chunk_id)
        return node._nparr


    def __setitem__(self, chunk_id, arr):
        if not isinstance(arr, numpy.ndarray):
            raise TypeError("Expected numpy array")
        if not isinstance(chunk_id, str):
            raise TypeError("Expected string type")
        if len(chunk_id) < 38:  
            # id should be prefix (e.g. "c-") and uuid value + chunk_index
            raise ValueError("Unexpected id length")
        if not chunk_id.startswith("c-"):
            raise ValueError("Unexpected prefix")
        if chunk_id in self._hash:
            raise KeyError("item already present in chunk cache")

        node = ChunkNode(chunk_id, arr)
        if self._lru_head is None:
            self._lru_head = self._lru_tail = node
        else:
            # newer items go to the front
            next = self._lru_head
            if next._prev is not None:
                raise KeyError("unexpected error")
            node._next = next
            next._prev = node  
            self._lru_head = node

        self._hash[chunk_id] = node
        self._mem_size += node._mem_size
        log.info("added new node to cache: {} [{} bytes]".format(chunk_id, node._mem_size))
        
        if self._mem_size > self._mem_target:
            # set dirty temporarily so we can't remove this node in reduceCache 
            node._isdirty = True 
            self._reduceCache()
            node._isdirty = False
             
    def _reduceCache(self):
        # remove chunks from cache (if not dirty) until we are under memory mem_target
        log.info("reduceCache")
        
        node = self._lru_tail  # start from the back
        while node is not None:
            log.info("check node: {}".format(node._id))
            next = node._prev
            if not node._isdirty:
                log.info("removing node: {}".format(node._id))
                self.__delitem__(node._id)
                if self._mem_size < self._mem_target:
                    log.info("mem_sized reduced below target")
                    break
            else:
                log.info("chunk: {} is dirty".format(node._id))
                pass # can't remove dirty nodes
            node = next
        # done reduceCache

    def consistencyCheck(self):
        """ verify that the data structure is self-consistent """
        id_list = []
        dirty_count = 0
        mem_usage = 0
        # walk the LRU list
        node = self._lru_head
        while node is not None:
            id_list.append(node._id)
            if node._id not in self._hash:
                raise ValueError("node: {} not found in hash".format(node._id))
            if node._isdirty:
                dirty_count += 1
                if node._id not in self._dirty_set:
                    raise ValueError("expected to find id: {} in dirty set".format(node._id))
            mem_usage += node._mem_size
            if not isinstance(node._nparr, numpy.ndarray):
                raise TypeError("Expected numpy array")
            node = node._next
        # finish forward iteration
        if len(id_list) != len(self._hash):
            raise ValueError("unexpected number of elements in forward LRU list")
        if dirty_count != len(self._dirty_set):
            raise ValueError("unexpected number of dirty chunks")
        if mem_usage != self._mem_size:
            raise ValueError("unexpected memory size")
        # go back through list
        node = self._lru_tail
        pos = len(id_list)
        reverse_count = 0
        while node is not None:
            reverse_count += 1
            if pos == 0:
                raise ValueError("unexpected node: {}".format(node._id))
            if node._id != id_list[pos - 1]:
                raise ValueError("expected node: {} but found: {}".format(id_list[pos-1], node._id))
            pos -= 1
            node = node._prev
        if reverse_count != len(id_list):
            raise ValueError("elements in reverse list do not equal forward list")
        # done - consistencyCheck


    def setDirty(self, chunk_id):
        # setting dirty flag has the side effect of moving this node 
        # up in the LRU list
        log.info("set dirty cache node id: {}".format(chunk_id))
                   
        node = self._moveToFront(chunk_id)
        node._isdirty = True
        
        self._dirty_set.add(chunk_id)


    def clearDirty(self, chunk_id):
        # clearing dirty flag has the side effect of moving this node 
        # up in the LRU list
        # also, may trigger a memory cleanup
        log.info("clear dirty for node:    {}".format(chunk_id))
        node = self._moveToFront(chunk_id)
        node._isdirty = False
        self._dirty_set.remove(chunk_id)
        if self._mem_size > self._mem_target:
            # maybe we can free up some memory now
            self._reduceCache()

    def isDirty(self, chunk_id):
        # don't adjust LRU position
        return chunk_id in self._dirty_set

    def dump_lru(self):
        """ Return LRU list as a string
            (for debugging)
        """
        node = self._lru_head
        s = "->"
        while node:
            s += node._id
            node = node._next
            if node:
                s +=  ","
        node = self._lru_tail
        s += "\n<-"
        while node:
            s += node._id
            node = node._prev
            if node:
                s +=  ","
        s += "\n"
        return s
    
    @property
    def cacheUtilizationPercent(self):
        return int((self._mem_size/self._mem_target)*100.0)

    @property
    def dirtyCount(self):
        return len(self._dirty_set)

    @property
    def memUsed(self):
        return self._mem_size

    @property
    def memTarget(self):
        return self._mem_target
