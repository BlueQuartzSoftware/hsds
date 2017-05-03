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
import time
import json

from aiohttp.web import StreamResponse, run_app

from aiohttp.errors import HttpProcessingError

import config
from basenode import baseInit, healthCheck
from util.timeUtil import unixTimeToUTC
from util.httpUtil import  jsonResponse
from util.s3Util import  getS3Keys, getS3JSONObj, getS3Bytes, putS3Bytes, isS3Obj
from util.idUtil import getCollectionForId, getS3Key
from util.chunkUtil import getDatasetId #, getChunkIndex
import hsds_logger as log

async def listKeys(app):
    """ Get all s3 keys in the bucket and create list of objkeys and domain keys """
    log.info("listKeys start")
    # Get all the keys for the bucket
    # request include_stats, so that for each key we get the ETag, LastModified, and Size values.
    s3keys = await getS3Keys(app, include_stats=True)
    log.info("got: {} keys".format(len(s3keys)))
    domains = {}
    groups = {}
    datasets = {}
    datatypes = {}
    top_level_domains = {}
    group_cnt = 0
    dset_cnt = 0
    datatype_cnt = 0
    chunk_cnt = 0
    domain_cnt = 0
    other_cnt = 0
    # 24693-g-ccd7e104-f86c-11e6-8f7b-0242ac110009
    for s3key in s3keys:
        log.info("next key: {}".format(s3key))
        if len(s3key) >= 44 and s3key[0:5].isalnum() and s3key[5] == '-' and s3key[6] in ('g', 'd', 'c', 't'):
            objid = s3key[6:]
            item = s3keys[s3key]  # Dictionary of ETag, LastModified, and Size
            item["used"] = False   # Mixin "Used" flag of false
            if objid[0] == 'g':
                groups[objid] = item
                group_cnt += 1
            elif objid[0] == 'd':
                # add a cunks dictionary that we'll use to store chunk keys later
                item["chunks"] = {}
                datasets[objid] = item
                dset_cnt += 1
            elif objid[0] == 't':
                datatypes[objid] = item
                datatype_cnt += 1
            elif objid[0] == 'c':
                chunk_cnt += 1
        elif s3key == "headnode":
            pass
        elif s3key.endswith(".txt"):
            # ignore collection files
            pass
        elif s3key.endswith("/.domain.json"):
            n = s3key.index('/')
            if n == 0:
                log.warn("unexpected domain name (leading slash): {}".format(s3key))
            elif n == -1:
                log.warn("unexpected domain name (no slash): {}".format(s3key))
            else:
                tld = s3key[:n]
                if tld not in top_level_domains:
                    top_level_domains[tld] = {}
                domain_cnt += 1
                # TBD - add a domainUtil func for this
                domain = '/' + s3key[:-(len("/.domain.json"))]
                domains[domain] = {}
                #domains[domain] = {"groups": {}, "datasets": {}, "datatypes": {}}
            
        else:
            log.warn("unknown object: {}".format(s3key))
    log.info("domain_cnt: {}".format(domain_cnt))
    log.info("group_cnt: {}".format(group_cnt))
    log.info("dset_cnt: {}".format(dset_cnt))
    log.info("datatype_cnt: {}".format(datatype_cnt))
    log.info("chunk_cnt: {}".format(chunk_cnt))
    log.info("other_cnt: {}".format(other_cnt))
    log.info("top_level_domains:")
    for tld in top_level_domains:
        log.info(tld)    
    
    app["domains"] = domains
    app["groups"] = groups
    app["datasets"] = datasets
    app["datatypes"] = datatypes

    chunk_del = []  # list of chunk ids that no longer have a dataset

    # iterate through s3keys again and add any chunks to the corresponding dataset
    for s3key in s3keys:
        if len(s3key) >= 44 and s3key[0:5].isalnum() and s3key[5] == '-' and s3key[6] == 'c':
            chunk_id = s3key[6:]
            dset_id = getDatasetId(chunk_id)
            if dset_id not in datasets:
                chunk_del.append(chunk_id)
            else:
                item = s3keys[s3key]  # Dictionary of ETag, LastModified, and Size
                dset = datasets[dset_id]
                dset_chunks = dset["chunks"]
                dset_chunks[chunk_id] = item

    log.info("chunk delete list ({} items):".format(len(chunk_del)))
    for chunk_id in chunk_del:
        #log.info(chunk_id)
        pass

    log.info("listKeys done")

async def markObj(app, domain_obj, obj_id):
    """ Mark obj as in-use and for group objs, recursively call for hardlink objects 
    """
    log.info("markObj: {}".format(obj_id))
    collection = getCollectionForId(obj_id)
    obj_ids = domain_obj[collection]
    if obj_id not in obj_ids:
        log.warn("markObj: key {} not found s3_key: {}".format(obj_id, getS3Key(obj_id)))
        return
    log.info("markObj: {}".format(obj_id))
    obj = obj_ids[obj_id]
    if obj["used"]:
        # we must have already visited this object and its children before
        # i.e. through a loop in the graph, so just return here
        return
    obj["used"] = True  # in use
    if collection == "groups":
        # add the objid to our domain list by collection type
        s3key = getS3Key(obj_id)
        try:
            data = await getS3Bytes(app, s3key)
        except HttpProcessingError as hpe:
            log.error("Error {} reading S3 key: {} ".format(hpe.code, s3key))
            return
        obj_json = json.loads(data.decode('utf8'))
        if "domain" not in obj_json:
            log.warn("Expected to find domain key for obj: {}".format(obj_id))
            return
    domain = obj_json["domain"]
    domains = app["domains"]
    if domain in domains:
        domain_obj = domains[domain] 
        domain_col = domain_obj[collection]
        domain_col[obj_id] = obj
        log.info("added {} to domain collection: {}".format(obj_id, collection))
    else:
        log.warn("domain {} for group: {} not found".format(domain, obj_id))
    if collection == "groups":
        # For group objects, iteratore through all the hard lines and mark those objects
        links = obj_json["links"]
        for link_name in links:
            link_json = links[link_name]
            if link_json["class"] == "H5L_TYPE_HARD":
                link_id = link_json["id"]
                await markObj(app, link_id)  # recursive call

async def initializeDomainCollections(app, domain, objid=None):
    """ File in the domain structure by recrusively reading each group and 
    add any hardlinked items to the appropriate collection.
    """
    log.info("initializeDomainCollections for domain: {}".format(domain))
    domains = app["domains"]
    if domain not in domains:
        log.warn("initializeDomainCollections - didn't find domain: {} in domains".format(domain))
        return
    domain_obj = domains[domain]
     
    # if no objid, start with the root
    if objid is None:
        s3key = getS3Key(domain)
        obj_json = await getS3JSONObj(app, s3key)
        if "root" not in obj_json:
            msg = "no root for {}".format(domain)
            log.info("no root for {} (domain folder)".format(domain))
            return
        # create groups, datasets, and datatypes collection
        domain_obj["groups"] = {}
        domain_obj["datasets"] = {}
        domain_obj["datatypes"] = {}
        objid = obj_json["root"]
        log.info("{} root: {}".format(domain, objid))

    collection = getCollectionForId(objid)
    domain_collection = domain_obj[collection]
    if objid in domain_collection:
        # we've already processed this object
        return

    # check to see if the id is in the global collection
    global_collection = app[collection]
    if objid not in global_collection:
        msg = "Expected to find: {} in global collection".format(objid)
        log.warn(msg)
        raise KeyError(msg)
    obj = global_collection[objid]
    domain_collection[objid] = obj  # reference object in domain collection
    if collection == "groups":
        # recurse through hard links in group objecct
        group_json = await getS3JSONObj(app, getS3Key(objid))
        if "links" not in group_json:
            msg = "Expected to find links member of group: {}".format(objid)
            log.error(msg)
            raise KeyError(msg)
        links = group_json["links"]
        for link_name in links:
            link_json = links[link_name]
            if link_json["class"] == "H5L_TYPE_HARD":
                link_id = link_json["id"]
                await initializeDomainCollections(app, domain, link_id)  # recursive call
 


async def markAndSweep(app, domain):
    """ Implement classic mark and sweep algorithm. """
    domains = app["domains"]
    if domain not in domains:
        log.warn("markAndSweep - didn't find domain: {} in domains".format(domain))
        return
    domain_obj = domains[domain]
    groups = domain_obj["groups"]
    datasets = domain_obj["datasets"]
    datatypes = domain_obj["datatypes"]
    log.info("markAndSweep start")
    
    # now iterate through domain
    log.info("mark domain objects start")
    s3key = getS3Key(domain)
    root_id = None
    try:
        obj_json = await getS3JSONObj(app, s3key)
        if "root" not in obj_json:
            log.info("no root for {}".format(domain))
        else:
            root_id = obj_json["root"]
            log.info("{} root: {}".format(domain, root_id))
            await markObj(app, root_id)  # this will recurse through object tree
    except HttpProcessingError:
        log.warn("domain object: {} not found".format(s3key))
    log.info("mark domain objects done for domain: {}".format(domain))

    # delete any objects that are not in use
    log.info("delete unmarked objects start for domain: {}".format(domain))
    delete_count = 0
    for objid in groups:
        obj = groups[objid]
        if obj["used"] is False:
            delete_count += 1
            log.info("delete {}".format(objid))
    for objid in datatypes:
        obj = datatypes[objid]
        if obj["used"] is False:
            delete_count += 1
            log.info("delete {}".format(objid))
    for objid in datasets:
        obj = datasets[objid]
        if obj["used"] is False:
            chunks = obj["chunks"]
            for chunkid in chunks:
                log.info("delete {}".format(chunkid))
    log.info("delete unmarked objects done")

async def updateDatasetContents(app, domain, dsetid):
    """ Create a object listing all the chunks for given dataset
    """
    log.info("updateDatasetContents: {}".format(dsetid))
    datasets = app["datasets"]
    if dsetid not in datasets:
        log.error("expected to find dsetid")
        return
    dset_obj = datasets[dsetid]
    chunks = dset_obj["chunks"]
    if len(chunks) == 0:
        log.info("no chunks for dataset")
        return
    # TBD: Replace with domainUtil func
    col_s3key = domain[1:] + "/." + dsetid + ".chunks.txt"  
    if await isS3Obj(app, col_s3key):
        # contents already exist, return
        # TBD: Add an option to force re-creation of index?
        #return
        pass
         
    chunk_ids = list(chunks.keys())
    chunk_ids.sort()
    text_data = b""
    for chunk_id in chunk_ids:
        log.info("getting chunk_obj for {}".format(chunk_id))
        chunk_obj = chunks[chunk_id]
        # chunk_obj should have keys: ETag, Size, and LastModified 
        if "ETag" not in chunk_obj:
            log.warn("chunk_obj for {} not initialized".format(chunk_id))
            continue
        line = "{} {} {} {}\n".format(chunk_id[39:], chunk_obj["ETag"], chunk_obj["LastModified"], chunk_obj["Size"])
        log.info("chunk contents: {}".format(line))
        line = line.encode('utf8')
        text_data += line
    log.info("write chunk collection key: {}, count: {}".format(col_s3key, len(chunk_ids)))
    try:
        await putS3Bytes(app, col_s3key, text_data)
    except HttpProcessingError:
        log.error("S3 Error writing chunk collection key: {}".format(col_s3key))
  

async def updateDomainContent(app, domain):
    """ Create/update context files listing objids and size for objects in the domain.
    """
    log.info("updateDomainContent: {}".format(domain))
     
    domains = app["domains"]
    log.info("{} domains".format(len(domains)))
     
    domain_obj = domains[domain]
    # for folder objects, the domain_obj won't have a groups key
    if "groups" not in domain_obj:
        return  # just a folder domain
    for collection in ("groups", "datatypes", "datasets"):
        domain_col = domain_obj[collection]
        log.info("domain_{} count: {}".format(collection, len(domain_col)))
        col_s3key = domain[1:] + "/." + collection + ".txt"  
        if await isS3Obj(app, col_s3key):
            # Domain collection already exist
            # TBD: add option to force re-creation?
            #continue
            pass
        if len(domain_col) > 0:
            col_ids = list(domain_col.keys())
            col_ids.sort()
            text_data = b""
            for obj_id in col_ids:
                col_obj = domain_col[obj_id]
                line = "{} {} {} {}\n".format(obj_id, col_obj["ETag"], col_obj["LastModified"], col_obj["Size"])
                line = line.encode('utf8')
                text_data += line
                if getCollectionForId(obj_id) == "datasets":
                    # create chunk listing
                    await updateDatasetContents(app, domain, obj_id)
            log.info("write collection key: {}, count: {}".format(col_s3key, len(col_ids)))
            try:
                await putS3Bytes(app, col_s3key, text_data)
            except HttpProcessingError:
                log.error("S3 Error writing {}.json key: {}".format(collection, col_s3key))

    log.info("updateDomainContent: {} Done".format(domain))


async def bucketCheck(app):
    """ Periodic method that iterates through all keys in the bucket  
    """

    #initialize these objecs here rather than in main to avoid "ouside of coroutine" errors

    app["last_bucket_check"] = int(time.time())

    # update/initialize root object before starting node updates
 
    while True:  
        if app["node_state"] != "READY":
            log.info("bucketCheck waiting for Node state to be READY")
            await  asyncio.sleep(1)
        else:
            break

    now = int(time.time())
    log.info("bucket check {}".format(unixTimeToUTC(now)))
    # do initial listKeys
    await listKeys(app)
    groups = app["groups"]
    for key in groups:
        log.info("found group id: {}".format(key))

    domains = app["domains"]

    # do GC for all domains at startup
    for domain in domains:
        log.info("domain: {}".format(domain))
        
    cnt = 0
    for domain in domains:
        log.info("domain: {}".format(domain))
        # organize collections of groups/datasets/and datatypes for each domain
        try:
            await initializeDomainCollections(app, domain)
        except Exception as e:
            log.warn("got exception in initializeDomainCollections for domain: {}: {}".format(domain, e))
            continue
        # await markAndSweep(app, domain)
        # update the domain contents files
        try:
            await updateDomainContent(app, domain)
        except Exception  as e:
            log.warn("got exception in updateDomainContent for domain: {}: {}".format(domain, e))
            continue
        cnt += 1
        if cnt == 5:
            break

    while True:
        # sleep for a bit
        sleep_secs = config.get("async_sleep_time")
        await  asyncio.sleep(sleep_secs)
        

async def info(request):
    """HTTP Method to return node state to caller"""
    log.request(request) 
    app = request.app
    resp = StreamResponse()
    resp.headers['Content-Type'] = 'application/json'
    answer = {}
    # copy relevant entries from state dictionary to response
    answer['id'] = request.app['id']
    answer['start_time'] = unixTimeToUTC(app['start_time'])
     
    resp = await jsonResponse(request, answer)
    log.response(request, resp=resp)
    return resp
 

async def init(loop):
    """Intitialize application and return app object"""
    
    app = baseInit(loop, 'an')

    app.router.add_get('/', info)
    app.router.add_get('/info', info)
    
    
    return app

#
# Main
#

if __name__ == '__main__':
    log.info("AsyncNode initializing")
    
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(init(loop))   
    # run background tasks
    asyncio.ensure_future(bucketCheck(app), loop=loop)
    asyncio.ensure_future(healthCheck(app), loop=loop)
    async_port = config.get("an_port")
    log.info("Starting service on port: {}".format(async_port))
    run_app(app, port=int(async_port))
