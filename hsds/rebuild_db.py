import asyncio
from os.path import isfile, exists, join
import sys
import sqlite3
from aiobotocore import get_session
from util.s3Util import getS3JSONObj, releaseClient, getS3Keys
from util.idUtil import isS3ObjKey, isValidUuid, isValidChunkId, getS3Key, getObjId, getCollectionForId
from util.domainUtil import isValidDomain
from util.dbutil import  insertRow, batchInsertChunkTable, insertTLDTable, listObjects, getDatasetChunks, updateRowColumn, dbInitTable
import hsds_logger as log
import config

async def getRootProperty(app, objid):
    """ Get the root property if not already set """
    log.debug("getRootProperty {}".format(objid))
    
    if isValidDomain(objid):
        log.debug("got domain id: {}".format(objid))
    else:
        if not isValidUuid(objid) or isValidChunkId(objid):
            raise ValueError("unexpected key for root property: {}".format(objid))
    s3key = getS3Key(objid)
    obj_json = await getS3JSONObj(app, s3key)
    rootid = None
    if "root" not in obj_json:
        if isValidDomain(objid):
            log.info("No root for folder domain: {}".format(objid))
        else:
            log.error("no root for {}".format(objid))
    else:
        rootid = obj_json["root"]
        log.debug("got rootid {} for obj: {}".format(rootid, objid))
    return rootid

async def gets3keys_callback(app, s3keys):
    #
    # this is called for each page of results by getS3Keys 
    #
    
    log.debug("getS3Keys count: {} keys".format(len(s3keys)))   
    chunk_items = [] # batch up chunk inserts for faster processing 

    for s3key in s3keys:
        log.debug("got s3key: {}".format(s3key))
        if not isS3ObjKey(s3key):
            log.debug("ignoring: {}".format(s3key))
            continue

        item = s3keys[s3key]   
        id = getObjId(s3key)
        log.debug("object id: {}".format(id))
        
        etag = ''
        if "ETag" in item:
            etag = item["ETag"]
            if len(etag) > 2 and etag[0] == '"' and etag[-1] == '"':
                # for some reason the etag is quoated
                etag = etag[1:-1]
            
        lastModified = 0
        if "LastModified" in item:
            lastModified = int(item["LastModified"])
             
        objSize = 0
        if "Size" in item:
            objSize = item["Size"]

        app["objects_in_bucket"]  += 1
        app["bytes_in_bucket"] += objSize

        # save to a table based on the type of object this is
        if isValidDomain(id):
            rootid = await getRootProperty(app, id)  # get Root property from S3
            try:
                insertRow(conn, "DomainTable", id, etag=etag, lastModified=lastModified, objSize=objSize, rootid=rootid)
            except KeyError:
                log.warn("got KeyError inserting domain: {}".format(id))
                continue
            # add to Top-Level-Domain table if this is not a subdomain
            index = id[1:-1].find('/')  # look for interior slash - isValidDomain implies len > 2
            if index == -1:
                try:
                    insertTLDTable(conn, id)
                except KeyError:
                    log.warn("got KeyError inserting TLD: {}".format(id))
                    continue
        elif isValidChunkId(id):
            log.debug("Got chunk: {}".format(id))
            chunk_items.append((id, etag, objSize, lastModified))
        else:
            rootid = await getRootProperty(app, id)  # get Root property from S3
            collection = getCollectionForId(id)
            if collection == "groups":
                table = "GroupTable"
            elif collection == "datatypes":
                table = "TypeTable"
            elif collection == "datasets":
                table = "DatasetTable"
            else:
                log.error("Unexpected collection: {}".format(collection))
                continue
      
            try:
                insertRow(conn, table, id, etag=etag, lastModified=lastModified, objSize=objSize, rootid=rootid)
            except KeyError:
                log.error("got KeyError inserting object: {}".format(id))
                   
         
    # insert any remaining chunks
    if len(chunk_items) > 0:
        log.info("batchInsertChunkTable = {} items".format(len(chunk_items)))
        try:
            batchInsertChunkTable(conn, chunk_items)
        except KeyError:
            log.warn("got KeyError inserting chunk: {}".format(id))
            sys.exit(1)
    log.debug("gets3keys_callback done")


#
# List objects in the s3 bucket
#

async def listKeys(app):
    """ Get all s3 keys in the bucket and create list of objkeys and domain keys """
    
    log.info("listKeys start") 
    # Get all the keys for the bucket
    # request include_stats, so that for each key we get the ETag, LastModified, and Size values.
    await getS3Keys(app, include_stats=True, callback=gets3keys_callback)

    await asyncio.sleep(0)
    conn = app["conn"]
    
    roots = listObjects(conn)
    for rootid in roots:
        root = roots[rootid]
        log.info("root: {} -- {}".format(rootid, root))

        datasets = root["datasets"]
        for datasetid in datasets:
            dataset = datasets[datasetid]
            chunks = getDatasetChunks(conn, datasetid)
            for chunkid in chunks:
                chunk = chunks[chunkid]
                dataset["size"] += chunk["size"]
                if chunk["lastModified"] > dataset["lastModified"]:
                    dataset["lastModified"] = chunk["lastModified"]
            updateRowColumn(conn, "DatasetTable", "size", datasetid, dataset["size"], rootid=rootid)
            updateRowColumn(conn, "DatasetTable", "lastModified", datasetid, dataset["lastModified"], rootid=rootid)
            updateRowColumn(conn, "DatasetTable", "chunkCount", datasetid, len(chunks), rootid=rootid)
            root["chunkCount"] += len(chunks)
            root["size"] += dataset["size"]
            if dataset["lastModified"] > root["lastModified"]:
                root["lastModified"] = dataset["lastModified"]
        
        groups = root["groups"]
        for groupid in groups:
            group = groups[groupid]
            root["size"] += group["size"]
            if group["lastModified"] > root["lastModified"]:
                root["lastModified"] = group["lastModified"]

        datatypes = root["datatypes"]
        for typeid in datatypes:
            datatype = datatypes[typeid]
            root["size"] += datatype["size"]
            if datatype["lastModified"] > root["lastModified"]:
                root["lastModified"] = datatype["lastModified"]
        
        try:
            insertRow(conn, "RootTable", rootid, etag='', lastModified=root["lastModified"],
                objSize=root["size"], chunkCount=root["chunkCount"], groupCount=len(groups),
                datasetCount=len(datasets), typeCount=len(datatypes))
        except KeyError:
            log.warn("got KeyError inserting root: {}".format(rootid))
            continue
    listObjects(conn)

#
# Main
#

if __name__ == '__main__':
    log.info("rebuild_db initializing")
    
    # we need to setup a asyncio loop to query s3
    loop = asyncio.get_event_loop()
    
    app = {}
    app["bucket_name"] = config.get("bucket_name")
    db_dir = config.get("db_dir")
    if not db_dir:
        log.error("No database directory defined")
        sys.exit(-1)
    if not exists(db_dir):
        log.error("Database directory: {} does not exist".format(db_dir))
        sys.exit(-1)
    
    db_file = config.get("db_file")
    if not db_file:
        log.error("db_file not defined")
        sys.exit(-1)
    db_path = join(db_dir, db_file)
    log.info("db_path: {}".format(db_path))

    if isfile(db_path):
        log.error("Database already exists, delete first")
        sys.exit(-1)
    
    conn = sqlite3.connect(db_path)
    app["conn"] = conn
    app["bytes_in_bucket"] = 0
    app["objects_in_bucket"] = 0

    
    try:
        dbInitTable(conn)
    except sqlite3.OperationalError:
        print("Error creating db tables. Remove db file: {} and try again".format(db_file))
        sys.exit(-1)
    
    app["loop"] = loop

    session = get_session(loop=loop)
    app["session"] = session
    loop.run_until_complete(listKeys(app))
    releaseClient(app)
    loop.close()
    conn.close()

    print("done!")
    print("objects in bucket: {}".format(app["objects_in_bucket"]))
    print("bytes in bucket: {}".format(app["bytes_in_bucket"]))