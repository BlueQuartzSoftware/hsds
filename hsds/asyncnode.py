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
# Head node of hsds cluster
# 
import asyncio
from os.path import isfile, join
import time

from aiohttp.web import run_app, json_response
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound, HTTPConflict, HTTPInternalServerError, HTTPServiceUnavailable

import sqlite3

import config
from basenode import baseInit, healthCheck
from util.s3Util import deleteS3Obj
from util.chunkUtil import getDatasetId
from util.idUtil import isValidChunkId, isValidUuid, getCollectionForId, getS3Key   
from util.dbutil import getRow, getDomains, insertRow, deleteRow, updateRowColumn, listObjects, getCountColumnName, getDatasetChunks
import hsds_logger as log


    
#
# pending queue handler
#


async def objDelete(app, objid, rootid=None):
    """ Delete object from it's table and then delete the s3obj """
    log.info(f"objDelete: {objid}")
    if rootid:
        log.debug(f"rootid: {rootid}")
    conn = app["conn"]
    dbRow = getRow(conn, objid, rootid=rootid)
    if not dbRow:
        log.warn(f"obj: {objid} not found for deleteRow")
    else:
        log.info("deleting db row: {objid}")
        deleteRow(conn, objid, rootid=rootid)
    
    # delete the s3 obj
    s3_key = getS3Key(objid)
    log.info("deleting s3_key: {}".format(s3_key))
    try:
        await deleteS3Obj(app, s3_key)
        #TODO - keep track of deleted ids
    except HTTPNotFound:
        log.warn(f"got S3 error deleting obj_id: {objid} to S3, object not found")

    except HTTPInternalServerError as hpe:
        # S3 failure?
        log.warn(f"got S3 error deleting obj_id: {objid} to S3: {str(hpe)}")


    # get the rootentry from the root table
    if rootid:
        if "totalSize" in dbRow:
            # For dataset objs
            objSize = dbRow["totalSize"]
        else:
            objSize = dbRow["size"]
        try:
            rootEntry = getRow(conn, rootid, table="RootTable")
            if not rootEntry:
                msg = f"expected to find root: {rootid} in RootTable"
                log.warn(msg)
                raise KeyError(msg)
            log.debug("root row for {}: {}".format(rootid, rootEntry))
            domain_size = rootEntry["totalSize"]
            if domain_size and objSize:
                domain_size -= objSize
                if domain_size < 0:
                    log.warn("got negative totalSize for root: {}".format(rootid))
                else:
                    updateRowColumn(conn, rootid, "totalSize", domain_size, table="RootTable")
            # adjust the object count in root table
            col_name = getCountColumnName(objid)
            if col_name:
                object_count = rootEntry[col_name]
                log.debug("objDelete count: {} for {}".format(object_count, col_name))
                object_count -= 1 
                if object_count < 0:
                    log.warn("got invalid number of {} for root: {}".format(col_name, rootid))
                else:
                    updateRowColumn(conn, rootid, col_name, object_count, table="RootTable")
        except KeyError:
            # this will happen if the domain is being deleted
            log.info("No row for rootid: {} in RootTable".format(rootid))
        

            
 
async def processPendingQueue(app):
    """ Process any pending queue events """      
    DB_LIMIT=1000 # max number of rows to pull in one query
    pending_queue = app["pending_queue"]
    pending_count = len(pending_queue)
    conn = app["conn"]
    if pending_count == 0:
        return # nothing to do
    log.info("processPendingQueue start - {} items".format(pending_count))    
     
    # TBD - this could starve other work if items are getting added to the pending
    # queue continually.  Copy items off pending queue synchronously and then process?
    while len(pending_queue) > 0:
        log.debug("pending_queue len: {}".format(len(pending_queue)))
        log.debug("pending queue: {}".format(pending_queue))
        objid = pending_queue.pop(0)  # remove from the front
        log.debug("pop from pending queue: obj: {}".format(objid))
        collection = getCollectionForId(objid)
        if collection == "groups":
            # this should be a root group
            rootid = objid
            dbRow = getRow(conn, objid, table="RootTable")
            if dbRow:
                log.error("RootTable row not expected for id: {}".format(rootid))
                continue
            domain_objs = listObjects(conn, rootid=rootid, limit=DB_LIMIT)
            for domain_obj in domain_objs:
                log.info("domain delete: Item: {}".format(domain_obj))

        elif collection == "datasets":
            # remove any chunks in the pending queue
            chunk_rows = getDatasetChunks(conn, objid, limit=DB_LIMIT)
            log.debug("getDatasetChunks returned: {} rows".format(len(chunk_rows)))
            for chunk_id in chunk_rows:
                s3_key = getS3Key(chunk_id)
                log.info("deleting s3_key: {}".format(s3_key))
                try:
                    await deleteS3Obj(app, s3_key)
                except HTTPNotFound:
                    log.warn(f"got S3 error deleting chunk: {objid} to S3, object not found")
                    continue

                except HTTPInternalServerError as hpe:
                    # S3 failure?
                    log.warn(f"got S3 error deleting chunk: {objid} to S3: {str(hpe)}")
                    continue

                deleteRow(conn, chunk_id)
                # TO DO - adjust dataset and domain totalSize
            if len(chunk_rows) == DB_LIMIT:
                # there maybe more chunks to delete, so re-add to pending queue
                log.info("Adding {} to pending queue".format(objid))
                pending_queue.append(objid) 
        else:
            log.error("unexpected collection type for: {}".format(objid))
      
 

async def bucketCheck(app):
    """ Periodic method for GC and pending queue updates 
    """
 
    app["last_bucket_check"] = int(time.time())

    async_sleep_time = config.get("async_sleep_time")
    log.info("async_sleep_time: {}".format(async_sleep_time))
     
    # update/initialize root object before starting node updates
 
    while True:  
        if app["node_state"] != "READY":
            log.info("bucketCheck waiting for Node state to be READY")
            await asyncio.sleep(1)
            continue  # wait for READY state
        
        pending_queue = app["pending_queue"]
        if len(pending_queue) > 0:
            
            try:
                await processPendingQueue(app)
            except Exception as e:
                log.warn("bucketCheck - got exception from processPendingQueue: {}".format(e))

        
        await asyncio.sleep(async_sleep_time)   

    # shouldn't ever get here     
    log.error("bucketCheck terminating unexpectedly")
     
def updateBucketStats(app):  
    """ Collect some high level stats for use by the info request """
    bucket_stats = app["bucket_stats"]
    bucket_stats["object_count"] = 42
    """
    s3objs = app["s3objs"]
    domains = app["domains"]
    roots = app["roots"]
    deleted_ids = app["deleted_ids"]
    pending_queue = app["pending_queue"]
    
    bucket_stats["object_count"] = len(s3objs)  
    bucket_stats["domain_count"] = len(domains)
    bucket_stats["root_count"] = len(roots)
    bucket_stats["storage_size"] = app["bytes_in_bucket"]
    bucket_stats["pending_count"] = len(pending_queue)    
    bucket_stats["deleted_count"] = len(deleted_ids)
    """
        

async def GET_AsyncInfo(request):
    """HTTP Method to retun async node state to caller"""
    log.request(request)
    app = request.apps
    updateBucketStats(app)
    answer = {}
    answer["bucket_stats"] = app["bucket_stats"]
    resp = json_response(answer)
    log.response(request, resp=resp)
    return resp

async def PUT_Objects(request):
    """HTTP method to notify creation/update of objid"""
    log.request(request)
    app = request.app
    log.info("PUT_Objects")
    conn = app["conn"]
    if not conn:
        msg = "db not initizalized"
        log.warn(msg)
        raise HTTPServiceUnavailable()

    if not request.has_body:
        msg = "PUT objects with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    body = await request.json()
    log.debug("Got PUT Objects body: {}".format(body))
    if "objs" not in body:
        msg = "expected to find objs key in body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    objs = body["objs"]
    for obj in objs:
        log.debug("PUT_Objects, obj: {}".format(obj))
        if "id" not in obj:
            log.error("Expected id in PUT_Objects request")
            continue
        if "lastModified" not in obj:
            log.error("Expected lastModified in PUT_Objects request for id: {}".format(obj["id"]))
            continue
        if "size" not in obj:
            log.error("Expected size in PUT_Objects request for id: {}".format(obj["id"]))
            continue
        
        objid = obj["id"]
        if not isValidUuid(objid):
            log.info("Ignoring non-uuid id: {} for PUT_Objects".format(objid))
            continue

        lastModified = obj["lastModified"]
        etag = ''
        if "etag" in obj:
            etag = obj["etag"]
        if "root"  in obj:
            rootid = obj["root"]
        else:
            rootid = ''
        if "size" in obj:
            objSize = obj["size"]
        else:
            objSize = 0

 
        if not isValidChunkId(objid) and not rootid:
            # root id is required for all non-chunk updates
            log.error("no rootid provided for obj: {}".format(objid))
            continue
        if rootid:
            log.debug(f"using rootid: {rootid}")

        domain_size_delta = 0 
        dbRow = getRow(conn, objid, rootid=rootid)
        if isValidChunkId(objid):
            # get the dset row for this chunk
            dsetid = getDatasetId(objid)
            log.debug("dsetid for chunk: {}".format(dsetid))
            try:
                dset_row = getRow(conn, dsetid, rootid=rootid)
            except ValueError as ve:
                # TBD - logic looks broken here... how to get the root id
                log.warn(f"no rootid for chunk: {objid}")
                continue
            log.debug("for {} got dset_row: {}".format(dsetid, dset_row))
            allocated_size_delta = objSize
            if not dset_row:
                log.warn("dset: {} not found in DatasetTable - deleted?".format(dsetid))
            if dbRow:
                # existing chunk is being updated - update dataset size and lastModified
                if objSize and "totalSize" in dbRow:
                    allocated_size_delta -= dbRow["totalSize"]
            else:
                # new chunk - update number of chunks
                chunkCount = dset_row["chunkCount"] + 1
                log.debug("update chunkCount for {} to {}".format(dsetid, chunkCount))
                updateRowColumn(conn, dsetid, "chunkCount", chunkCount, rootid=rootid)
            
            updateRowColumn(conn, dsetid, "lastModified", lastModified, rootid=rootid)
            if allocated_size_delta != 0:
                if dbRow and "totalSize" in dbRow:
                    allocated_size = dbRow["totalSize"] + allocated_size_delta
                else:
                    allocated_size = allocated_size_delta
                log.debug("udpate totalSize for {} to {}".format(dsetid, allocated_size))
                updateRowColumn(conn, dsetid, "totalSize", allocated_size, rootid=rootid)
                log.debug("udpate chunkCount for {} to {}".format(dsetid, chunkCount))
                updateRowColumn(conn, dsetid, "chunkCount", chunkCount, rootid=rootid)
                domain_size_delta = allocated_size_delta # for when we update domain size below        
        else:
            # non-chunk update 
            if not dbRow:
                # insert new object
                log.info("insertRow for {}".format(objid))
                try:
                    insertRow(conn, objid, etag=etag, lastModified=lastModified, objSize=objSize, rootid=rootid)
                except KeyError:
                    log.error("got KeyError inserting object: {}".format(id))
                    continue  
                # if this is a new root group, add to root table
                if getCollectionForId(objid) == "groups" and objid == rootid:
                    try:
                        insertRow(conn, rootid, etag=etag, lastModified=lastModified, objSize=objSize, table="RootTable")            
                    except KeyError:
                        log.error("got KeyError inserting root: {}".format(rootid))
                        continue
                    updateRowColumn(conn, rootid, "totalSize", objSize, table="RootTable")
                elif getCollectionForId(objid) == "datasets":
                    updateRowColumn(conn, objid,  "totalSize", 0, rootid=rootid)
                    updateRowColumn(conn, objid,  "chunkCount", 0, rootid=rootid)
                    domain_size_delta = objSize
                else:
                    # new group or datatype
                    domain_size_delta = objSize

            else:
                # update existing object
                log.info("updateRow for {}".format(objid))
                updateRowColumn(conn, objid,  "lastModified", lastModified, rootid=rootid)
                if objSize:
                    updateRowColumn(conn, objid, "size", objSize, rootid=rootid)
                if etag:
                    updateRowColumn(conn, objid, "etag",  etag, rootid=rootid)
                domain_size_delta -= dbRow["size"]
        
        
        # update size and lastModified in root table
        try:
            rootEntry = getRow(conn, rootid, table="RootTable")
        except KeyError:
            log.error("Unable to find {} in RootTable".format(rootid))
            continue
        if not rootEntry:
            log.error("Unable to find {} in RootTable".format(rootid))
            continue
        if "totalSize" not in rootEntry:
            log.error("Expected to find size in RootTable for root: {}".format(rootid))
            continue
        
        if domain_size_delta != 0:
            domain_size = domain_size_delta
            if "totalSize" in rootEntry and rootEntry["totalSize"]:
                domain_size += rootEntry["totalSize"]
            updateRowColumn(conn, rootid, "totalSize", domain_size, table="RootTable")
        # update lastModified timestamp
        updateRowColumn(conn, rootid, "lastModified", lastModified, table="RootTable")
        if not dbRow:
            # new object, update object count in root table
            col_name = getCountColumnName(objid)
            count = rootEntry[col_name] + 1
            updateRowColumn(conn, rootid, col_name, count, table="RootTable")
        # go on to next obj
         

    resp_json = {  } 
    resp = json_response(resp_json, status=201)
    log.response(request, resp=resp)
    return resp

async def DELETE_Objects(request):
    """ HTTP method to notify deletion of objid """
    log.request(request)
    app = request.app
    pending_queue = app["pending_queue"]
    log.info("DELETE_Objects")

    if not request.has_body:
        msg = "DELETE objects with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    body = await request.json()
    if "objs" not in body:
        msg = "expected to find objs key in body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    objs = body["objs"]
    for obj in objs:
        if "id" not in obj:
            log.error("Expected id in PUT_Objects request")
            continue
        
        objid = obj["id"]
         
        log.info("Delete for objid: {}".format(objid))
        if  not isValidUuid(objid):
            msg = "DELETE_Objects Invalid id: {}".format(objid)
            log.error(msg)
            continue

        if isValidChunkId(objid):
            log.error("Chunks should not be explicitly deleted")
            continue

        if not isValidChunkId(objid) and not "root" in obj:
            # root id is required for all non-chunk updates
            log.error("no rootid provided for obj: {}".format(objid))
            continue

        rootid = obj["root"]
        log.debug(f"obj: {objid} root: {rootid}")
 
        dbRow = getRow(conn, objid, rootid=rootid)
        if not dbRow:
            log.warn("obj: {} not found for deleteRow")

        if objid == rootid:
            log.info("deleting root object row: {}".format(objid))
            deleteRow(conn, rootid, table="RootTable")

        log.info("deleting row: {}".format(objid))
        await objDelete(app, objid, rootid=rootid)

        if getCollectionForId(objid) == "datasets":
            # add object to pending queue to delete all chunks for this dataset
            log.info("adding dataset: {} to pending queue for chunk removal".format(objid))
            pending_queue.append(objid)
        elif objid == rootid:
            # add id to pending queue to delete all objects in this domain
            log.info("adding root: {} to pending queue for domain cleanup".format(objid))
            pending_queue.append(objid)
  
    resp_json = {  } 
    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp


async def GET_Object(request):
    """HTTP method to get object s3 state """
    log.request(request)
    app = request.app
    log.info("GET_Object")
    conn = app["conn"]
    if not conn:
        msg = "db not initizalized"
        log.warn(msg)
        raise HTTPServiceUnavailable()
    

    objid = request.match_info.get('id')
    params = request.rel_url.query
    if "Root" in params:
        rootid = params["Root"]
    else:
        rootid = ''
    resp_json = getRow(conn, objid, rootid=rootid)
    if not resp_json:
        msg = "objid: {} not found".format(objid)
        log.warn(msg)
        raise HTTPNotFound()

    log.info("GET_Object response: {}".format(resp_json))
    
    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp

async def GET_Domains(request):
    """HTTP method to get object s3 state """
    log.request(request)
    
    app = request.app
    params = request.rel_url.query
    if "prefix" not in params:
        msg = "No domain prefix provided"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    prefix = params["prefix"]

    verbose = False
    if "verbose" in params and params["verbose"]:
        log.debug("params[verbose]: {}".format(params["verbose"]))
        if int(params["verbose"]):
            verbose = True 

    log.info("GET_Domains: {} verbose={}".format(prefix, verbose))

    limit = None
    if "Limit" in params:
        try:
            limit = int(params["Limit"])
            log.debug("GET_Domains - using Limit: {}".format(limit))
        except ValueError:
            msg = "Bad Request: Expected int type for limit"
            log.error(msg)  # should be validated by SN
            raise HTTPBadRequest(reason=msg)
    marker = None
    if "Marker" in params:
        marker = params["Marker"]
        log.debug("got Marker request param: {}".format(marker))

    if not prefix.startswith("/"):
        msg = "Prefix expected to start with /"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if not prefix.endswith("/"):
        msg = "Prefix expected to end with /"

    conn = app["conn"]
    if not conn:
        msg = "db not initizalized"
        log.warn(msg)
        raise HTTPServiceUnavailable()

    domains = getDomains(conn, prefix, limit=limit, marker=marker)

    # for verbose copy in totalSize, num groups/datasets/datatypes, lastModified for each domain
    # otherwise jsut return name, owner and root
    for domain in domains:
        log.debug("domain: {}".format(domain))
        if "root" in domain and domain["root"] and domain["root"] != "None":
            log.info("domain: {} root: {}".format(domain, domain["root"]))
            domain["class"] = "domain"
        
            if verbose:
                dbRow = getRow(conn, domain["root"], table="RootTable")
                if not dbRow:
                    log.warn("missing RootTable row for id: {}".format(domain["root"]))
                    continue
                domain["size"] = dbRow["totalSize"]
                domain["lastModified"] = dbRow["lastModified"]
                domain["chunkCount"] = dbRow["chunkCount"]
                domain["groupCount"] = dbRow["groupCount"]
                domain["datasetCount"] = dbRow["datasetCount"]
                domain["typeCount"] = dbRow["typeCount"]
        else:
            domain["class"] = "folder"
        
        if not verbose:
            if "lastModified" in domain:
                del domain["lastModified"]
            if "size" in domain:
                del domain["size"]
        
    resp_json = {"domains": domains}
    log.info("GET_Domains response: {}".format(resp_json))
    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp

async def PUT_Domain(request):
    """HTTP method to get object s3 state """
    log.request(request)
    
    app = request.app
    params = request.rel_url.query
    if "domain" not in params:
        msg = "No domain provided"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    domain = params["domain"]

    if not domain.startswith("/"):
        msg = "Domain expected to start with /"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if len(domain) < 2:
        msg = "Invalid domain"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    rootid=''
    if "root" in params:
        rootid = params["root"]

    owner=''
    if "owner" in params:
        owner = params["owner"]
   
    conn = app["conn"]
    if not conn: 
        msg = "db not initizalized"
        log.warn(msg)
        raise HTTPServiceUnavailable()

    dbRow = getRow(conn, domain)
    if dbRow:
        msg = "domain: {} already found in db".format(domain)
        log.warn(msg)
        raise HTTPConflict()

    try:
        log.info("inserting domain: {} to DomainTable".format(domain))
        insertRow(conn, domain, rootid=rootid, owner=owner)
    except KeyError:
        msg = "got KeyError inserting object: {}".format(id)
        log.error(msg)
        raise HTTPInternalServerError()

    dbRow = getRow(conn, domain)
    if dbRow:
        msg = "domain row: {}".format(dbRow)
        log.info(msg)
    else:
        msg = "Unexpected no domain row for: {}".format(domain)
        log.error(msg)
        raise HTTPInternalServerError()

    resp_json = {}
    resp = json_response(resp_json, status=201)
    log.response(request, resp=resp)
    return resp

async def DELETE_Domain(request):
    log.request(request)

    app = request.app
    params = request.rel_url.query
    if "domain" not in params:
        msg = "No domain provided"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    domain = params["domain"]

    if not domain.startswith("/"):
        msg = "Domain expected to start with /"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if len(domain) < 2:
        msg = "Invalid domain"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    conn = app["conn"]
    if not conn: 
        msg = "db not initizalized"
        log.warn(msg)
        raise HTTPServiceUnavailable()
  
    dbRow = getRow(conn, domain)
    if not dbRow:
        msg = "domain: {} not found in db".format(domain)
        log.warn(msg)
        raise HTTPNotFound()
    log.info("got domain row: {}".format(dbRow))

    try:
        deleteRow(conn, domain)
    except KeyError:
        msg = "got KeyError inserting domain: {}".format(domain)
        log.error(msg)
        raise HTTPInternalServerError()

    resp_json = {}
    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp


async def GET_Root(request):
    """HTTP method to get root object state """
    log.request(request)
    log.info("GET_Root")
    
    rootid = request.match_info.get('id')

    app = request.app
    conn = app["conn"]
    params = request.rel_url.query

    if not conn:
        msg = "db not initizalized"
        log.warn(msg)
        raise HTTPServiceUnavailable()
    resp_json = getRow(conn, rootid, table="RootTable")
    if not resp_json:
        msg = "object not found"
        log.warn(msg)
        raise HTTPNotFound()

    if "verbose" in params and params["verbose"]:
        resp_json["objects"] = listObjects(conn, rootid)
    log.info("GET_Root response: {}".format(resp_json))

    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp


async def init(loop):
    """Intitialize application and return app object"""
    
    app = baseInit(loop, 'an')
    app.router.add_route('GET', '/async_info', GET_AsyncInfo)
    app.router.add_route('PUT', '/objects', PUT_Objects)
    app.router.add_route('DELETE', '/objects', DELETE_Objects)
    app.router.add_route('GET', '/objects/{id}', GET_Object)
    app.router.add_route('GET', '/domains', GET_Domains)
    app.router.add_route('PUT', '/domain', PUT_Domain)
    app.router.add_route('DELETE', '/domain', DELETE_Domain)
    app.router.add_route('GET', '/root/{id}', GET_Root)
    app["bucket_stats"] = {}
    # object and domain updates will be posted here to be worked on offline
    app["pending_queue"] = [] 
     
    app["bytes_in_bucket"] = 0
    app["anonymous_ttl"] = int(config.get("anonymous_ttl"))
    log.info("anonymous_ttl: {}".format(app["anonymous_ttl"]))
    app["updated_domains"] = set()
     
    return app

#
# Main
#

if __name__ == '__main__':
    log.info("AsyncNode initializing")
    
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(init(loop))   

    # connect to db
    db_path = join(config.get("db_dir"), config.get("db_file"))
    log.info("db_file: {}".format(db_path))
    if isfile(db_path):
        # found db file, connect to it
        log.info("connecting to sqlite db")
        conn = sqlite3.connect(db_path)
        app["conn"] = conn
    else:
        log.info("no dbfile found")
        app['conn'] = None
        
        
    # run background tasks
    asyncio.ensure_future(bucketCheck(app), loop=loop) 
    asyncio.ensure_future(healthCheck(app), loop=loop)

    async_port = config.get("an_port")
    log.info("Starting service on port: {}".format(async_port))
    run_app(app, port=int(async_port))
