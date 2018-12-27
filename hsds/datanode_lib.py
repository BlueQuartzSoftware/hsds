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
#
# data node of hsds cluster
# 
import asyncio
import time 
from aiohttp.web_exceptions import HTTPGone, HTTPInternalServerError, HTTPBadRequest
from aiohttp.client_exceptions import ClientError

from util.idUtil import validateInPartition, getS3Key, isValidUuid, isValidChunkId, isSchema2Id, getRootObjId
from util.s3Util import getS3JSONObj, putS3JSONObj, putS3Bytes, isS3Obj, deleteS3Obj
from util.domainUtil import isValidDomain
from util.attrUtil import getRequestCollectionName
from util.httpUtil import http_put, http_delete
from util.chunkUtil import getDatasetId
from util.arrayUtil import arrayToBytes
from basenode import getAsyncNodeUrl
import config
import hsds_logger as log


def get_obj_id(request, body=None):
    """ Get object id from request 
        Raise HTTPException on errors.
    """

    obj_id = None
    collection = None
    app = request.app
    if body and "id" in body:
        obj_id = body["id"]
    else:
        collection = getRequestCollectionName(request) # returns datasets|groups|datatypes
        obj_id = request.match_info.get('id')

    if not obj_id:
        msg = "Missing object id"
        log.error(msg)
        raise HTTPInternalServerError()

    if not isValidUuid(obj_id, obj_class=collection):
        msg = "Invalid obj id: {}".format(obj_id)
        log.error(msg)
        raise HTTPInternalServerError()

    try:
        validateInPartition(app, obj_id)
    except KeyError as ke:
        msg = "Domain not in partition"
        log.error(msg)
        raise HTTPInternalServerError() 

    return obj_id   

async def check_metadata_obj(app, obj_id):
    """ Return False is obj does not exist
    """
    if not isValidDomain(obj_id) and not isValidUuid(obj_id):
        msg = "Invalid obj id: {}".format(obj_id)
        log.error(msg)
        raise HTTPInternalServerError()

    try:
        validateInPartition(app, obj_id)
    except KeyError as ke:
        msg = "Domain not in partition"
        log.error(msg)
        raise HTTPInternalServerError() 

    deleted_ids = app['deleted_ids']
    if obj_id in deleted_ids:
        msg = "{} has been deleted".format(obj_id)
        log.info(msg)
        return False
    
    meta_cache = app['meta_cache'] 
    if obj_id in meta_cache:
        found = True
    else:
        # Not in chache, check s3 obj exists   
        s3_key = getS3Key(obj_id)
        log.debug("check_metadata_obj({})".format(s3_key))
        # does key exist?
        found = await isS3Obj(app, s3_key)
    return found
    
 

async def get_metadata_obj(app, obj_id):
    """ Get object from metadata cache (if present).
        Otherwise fetch from S3 and add to cache
    """
    log.info("get_metadata_obj: {}".format(obj_id))
    if not isValidDomain(obj_id) and not isValidUuid(obj_id):
        msg = "Invalid obj id: {}".format(obj_id)
        log.error(msg)
        raise HTTPInternalServerError()

    try:
        validateInPartition(app, obj_id)
    except KeyError as ke:
        msg = "Domain not in partition"
        log.error(msg)
        raise HTTPInternalServerError() 

    deleted_ids = app['deleted_ids']
    if obj_id in deleted_ids:
        msg = "{} has been deleted".format(obj_id)
        log.warn(msg)
        raise HTTPGone() 
    
    meta_cache = app['meta_cache'] 
    obj_json = None 
    if obj_id in meta_cache:
        log.debug("{} found in meta cache".format(obj_id))
        obj_json = meta_cache[obj_id]
    else:   
        s3_key = getS3Key(obj_id)
        pending_s3_read = app["pending_s3_read"]
        if s3_key in pending_s3_read:
            # already a read in progress, wait for it to complete
            read_start_time = pending_s3_read[s3_key]
            log.info(f"s3 read request for {s3_key} was requested at: {read_start_time}")
            while time.time() - read_start_time < 2.0:
                log.debug("waiting for pending s3 read, sleeping")
                await asyncio.sleep(1)  # sleep for sub-second?
                if obj_id in meta_cache:
                    log.info(f"object {obj_id} has arrived!")
                    obj_json = meta_cache[obj_id]
                    break
            if not obj_json:
                log.warn(f"s3 read for object {s3_key} timed-out, initiaiting a new read")
        
        # invoke S3 read unless the object has just come in from pending read
        if not obj_json:
            log.debug("getS3JSONObj({})".format(s3_key))
            if s3_key not in pending_s3_read:
                pending_s3_read[s3_key] = time.time()
            # read S3 object as JSON
            obj_json = await getS3JSONObj(app, s3_key)
            if s3_key in pending_s3_read:
                # read complete - remove from pending map
                elapsed_time = time.time() - pending_s3_read[s3_key]
                log.info(f"s3 read for {s3_key} took {elapsed_time}")
                del pending_s3_read[s3_key] 
            meta_cache[obj_id] = obj_json  # add to cache
    return obj_json

async def write_s3_obj(app, obj_id):
    """ writes the given object to s3 """
    s3_key = getS3Key(obj_id)  
    log.info(f"s3sync for obj_id: {obj_id} / s3_key: {s3_key}")
    pending_s3_write = app["pending_s3_write"]
    chunk_cache = app['chunk_cache']
    meta_cache = app['meta_cache']
    deflate_map = app['deflate_map']

    if isValidChunkId(obj_id):
        # chunk update
        if obj_id not in chunk_cache:
            log.error("expected to find obj_id: {} in chunk cache".format(obj_id))
            raise KeyError(f"{obj_id} not found in chunk cache")
    else:
        # check for object in meta cache
        if obj_id not in meta_cache:
            log.error("expected to find obj_id: {} in meta cache".format(obj_id))
            raise KeyError(f"{obj_id} not found in chunk cache")

    if s3_key in pending_s3_write:
        # already a write in progress, wait for it to complete
        # to avoid any out of order issues
        log.warn(f"write_s3_obj({s3_key}) already in progress")
        write_start_time = pending_s3_write[s3_key]
        log.info(f"s3 write request for {obj_id} was requested at: {write_start_time}")
        while time.time() - write_start_time < 2.0:
            log.debug("waiting for pending s3 write, sleeping")
            await asyncio.sleep(1)  # sleep for sub-second?
            if s3_key not in pending_s3_write:
                log.info(f"object {obj_id} has been written!")
                pending_s3_write[s3_key] = time.time()
                break
    else:
        # add key to pending map
        log.debug(f"adding {s3_key} to pending_s3_write")
        write_start_time = time.time()
        pending_s3_write[s3_key] = write_start_time

    if isValidChunkId(obj_id):
        chunk_arr = chunk_cache[obj_id]
        chunk_cache.clearDirty(obj_id)  # chunk may get evicted from cache now
        chunk_bytes = arrayToBytes(chunk_arr)
        dset_id = getDatasetId(obj_id)
        deflate_level = None
        if dset_id in deflate_map:
            deflate_level = deflate_map[dset_id]
            log.debug("got deflate_level: {} for dset: {}".format(deflate_level, dset_id))
                    
        try:
            await putS3Bytes(app, s3_key, chunk_bytes, deflate_level=deflate_level)
        except HTTPInternalServerError as hpe:
            log.error("got S3 error writing obj_id: {} to S3: {}".format(obj_id, str(hpe)))
            # re-add chunk to cache if it had gotten evicted
            if obj_id not in chunk_cache:
                chunk_cache[obj_id] = chunk_arr
            chunk_cache.setDirty(obj_id)  # pin to cache  
            del pending_s3_write[s3_key] 
            raise # re-throw the execption  
        log.debug("putS3Bytes Chunk cache utilization: {} per, dirty_count: {}".format(chunk_cache.cacheUtilizationPercent, chunk_cache.dirtyCount))
      
    else:
        # meta data update     
        obj_json = meta_cache[obj_id]
        meta_cache.clearDirty(obj_id)
        try:
            await putS3JSONObj(app, s3_key, obj_json)                     
        except HTTPInternalServerError as hpe:
            log.error("got S3 error writing obj_id: {} to S3: {}".format(obj_id, str(hpe)))
            # re-add chunk to cache if it had gotten evicted
            if obj_id not in meta_cache:
                meta_cache[obj_id] = obj_json
            meta_cache.setDirty(obj_id)  # pin to cache 
            del pending_s3_write[s3_key] 
            raise # re-throw exception   

    if s3_key not in pending_s3_write:
        log.warn(f"expected to find {s3_key} in pending_s3_write map")
    else:
        # wite complete - record time and remove from pending map
        elapsed_time = time.time() - write_start_time
        log.info(f"s3 write for {s3_key} took {elapsed_time}")
        # clear pending write 
        del pending_s3_write[s3_key]        
            
   

async def save_metadata_obj(app, obj_id, obj_json, notify=False, flush=False):
    """ Persist the given object """
    log.info(f"save_metadata_obj {obj_id} notify={notify} flush={flush}")
    if not obj_id.startswith('/') and not isValidUuid(obj_id):
        msg = "Invalid obj id: {}".format(obj_id)
        log.error(msg)
        raise HTTPInternalServerError()
    if not isinstance(obj_json, dict):
        log.error("Passed non-dict obj to save_metadata_obj")
        raise HTTPInternalServerError() 

    try:
        validateInPartition(app, obj_id)
    except KeyError as ke:
        msg = "Domain not in partition"
        log.error(msg)
        raise HTTPInternalServerError() 

    deleted_ids = app['deleted_ids']
    if obj_id in deleted_ids:
        if isValidUuid(obj_id):
            # domain objects may be re-created, but shouldn't see repeats of 
            # deleted uuids
            log.warn("{} has been deleted".format(obj_id))
            raise HTTPInternalServerError() 
        elif obj_id in deleted_ids:
            deleted_ids.remove(obj_id)  # un-gone the domain id
    
    # update meta cache
    meta_cache = app['meta_cache'] 
    log.debug("save: {} to cache".format(obj_id))
    meta_cache[obj_id] = obj_json
    meta_cache.setDirty(obj_id)
    now = int(time.time())
    
    if flush:
        # write to S3 immediately
        if isValidChunkId(obj_id):
            log.warn("flush not supported for save_metadata_obj with chunks")
            raise HTTPBadRequest()
        try:
            await write_s3_obj(app, obj_id)
        except KeyError as ke:
            log.error(f"s3 sync got key error: {ke}")
            raise HTTPInternalServerError()
        except HTTPInternalServerError as hpe:
            log.warn(f" failed to write {obj_id}")
            raise  # re-throw                
    else:
        # flag to write to S3
        dirty_ids = app["dirty_ids"]
        dirty_ids[obj_id] = now

     
    # message AN immediately if notify flag is set
    # otherwise AN will be notified at next S3 sync
    if notify:
        an_url = getAsyncNodeUrl(app)

        if obj_id.startswith("/"):
            # domain update
            req = an_url + "/domain"
            params = {"domain": obj_id}
            if "root" in obj_json:
                params["root"] = obj_json["root"]
            if "owner" in obj_json:
                params["owner"] = obj_json["owner"]
            try:
                log.info("ASync PUT notify: {} params: {}".format(req, params))
                await http_put(app, req, params=params)
            except HTTPInternalServerError as hpe:
                log.error(f"got error notifying async node: {hpe}")
                log.error(msg)

        else:
            req = an_url + "/object/" + obj_id
            try:
                log.info("ASync PUT notify: {}".format(req))
                await http_put(app, req)
            except HTTPInternalServerError:
                log.error(f"got error notifying async node")
        


async def delete_metadata_obj(app, obj_id, notify=True, root_id=None):
    """ Delete the given object """
    meta_cache = app['meta_cache'] 
    dirty_ids = app["dirty_ids"]
    log.info("delete_meta_data_obj: {} notify: {}".format(obj_id, notify))
    if not isValidDomain(obj_id) and not isValidUuid(obj_id):
        msg = "Invalid obj id: {}".format(obj_id)
        log.error(msg)
        raise HTTPInternalServerError()
        
    try:
        validateInPartition(app, obj_id)
    except KeyError as ke:
        msg = "obj: {} not in partition".format(obj_id)
        log.error(msg)
        raise HTTPInternalServerError() 

    deleted_ids = app['deleted_ids']
    if obj_id in deleted_ids:
        log.warn("{} has already been deleted".format(obj_id))
    else:
        deleted_ids.add(obj_id)
     
    if obj_id in meta_cache:
        log.debug(f"removing {obj_id} from meta_cache")
        del meta_cache[obj_id]
    
    if obj_id in dirty_ids:
        del dirty_ids[obj_id]

    # remove from S3 (if present)
    s3key = getS3Key(obj_id)

    if await isS3Obj(app, s3key):
        await deleteS3Obj(app, s3key)
    else:
        log.info(f"delete_metadata_obj - key {s3key} not found (never written)?")
    
    if notify:
        an_url = getAsyncNodeUrl(app)
        if obj_id.startswith("/"):
            # domain delete
            req = an_url + "/domain"
            params = {"domain": obj_id}
            
            try:
                log.info("ASync DELETE notify: {} params: {}".format(req, params))
                await http_delete(app, req, params=params)
            except ClientError as ce:
                log.error(f"got error notifying async node: {ce}")
            except HTTPInternalServerError as hse:
                log.error(f"got HTTPInternalServerError: {hse}")
        else:
            req = an_url + "/object/" + obj_id
            try:
                log.info(f"ASync DELETE notify: {req}")
                await http_delete(app, req)
            except ClientError as ce:
                log.error(f"got ClientError notifying async node: {ce}")
            except HTTPInternalServerError as ise:
                log.error(f"got HTTPInternalServerError notifying async node: {ise}")
    log.debug(f"delete_metadata_obj for {obj_id} done")

    

async def s3syncCheck(app):
    sleep_secs = config.get("node_sleep_time")
    s3_sync_interval = config.get("s3_sync_interval")

    while True:
        if app["node_state"] != "READY":
            log.info("s3sync - clusterstate is not ready, sleeping")
            await asyncio.sleep(sleep_secs)
            continue
        # write all objects that have been updated more than s3_sync_interval ago
        age = time.time() - s3_sync_interval
        try:
            update_count = await s3sync(app, age)
            if update_count:
                log.info(f"s3syncCheck {update_count} objects updated")
            else:
                log.info("s3syncCheck no objects to write, sleeping")
                await asyncio.sleep(sleep_secs)

        except Exception as e:
            # catch all exception to keep the loop going
            log.error(f"Got Exception running s3sync: {e}")

async def s3sync(app, age, rootid=None):
    """ Periodic method that writes dirty objects in the metadata cache to S3"""
    log.info(f"s3sync( age={age}, rootid={rootid}")
    dirty_ids = app["dirty_ids"]
    update_count = None
        
    keys_to_update = []
    for obj_id in dirty_ids:
        if obj_id.startswith("/"):
            continue  # ignore domain ids
        if not isValidUuid(obj_id):
            log.warn(f"Unexpected objid in dirty_ids: {obj_id}")
            continue
        if dirty_ids[obj_id] > age:
            continue   # update was too recent, ignore for now
        if rootid and not isSchema2Id(obj_id):
            continue  # root collectino flush only works with v2 ids
        if rootid and getRootObjId(obj_id) != rootid:
            continue  # not in the collection we want to update
        keys_to_update.append(obj_id)
    
    if len(keys_to_update) == 0:
        return 0

    update_count = len(keys_to_update)
        
    # some objects need to be flushed to S3
    log.info(f"{update_count} objects to be synched to S3")

    # first clear the dirty id (before we hit the first await) to
    # avoid a race condition where the object gets marked as dirty again
    # (causing us to miss an update)
    for obj_id in keys_to_update:
        del dirty_ids[obj_id]
            
    retry_keys = []  # add any write failures back here
    notify_objs = []  # notifications to send to AN, also flags success write to S3
    for obj_id in keys_to_update:
        try:
            await write_s3_obj(app, obj_id)
            notify_objs.append(obj_id)
        except KeyError as ke:
            log.error(f"s3 sync got key error: {ke}")
            retry_keys.append(obj_id)
        except HTTPInternalServerError as hpe:
            log.warn(f"s3 sync - failed to write {obj_id} adding to retry list")
            retry_keys.append(obj_id)
            
    # add any failed writes back to the dirty queue
    if len(retry_keys) > 0:
        log.warn("{} failed S3 writes, re-adding to dirty set".format(len(retry_keys)))
        # we'll put the timestamp down as now, so the rewrites won't be triggered immediately
        now = int(time.time())
        for obj_id in retry_keys:
            dirty_ids[obj_id] = now

    # notify AN of key updates 
    an_url = getAsyncNodeUrl(app)
    log.info("Notifying AN for S3 Updates")
    if len(notify_objs) > 0:           
        body = { "objs": notify_objs }
        req = an_url + "/objects"
        try:
            log.info("ASync PUT notify: {} body: {}".format(req, body))
            await http_put(app, req, data=body)
        except HTTPInternalServerError as hpe:
            msg = "got error notifying async node: {}".format(hpe)
            log.error(msg)

    # return number of objects written
    return update_count
