#
# Head node of hsds cluster
# 
import json
import hashlib
from aiohttp.errors import  ClientOSError

def isOK(http_response):
    if http_response < 300:
        return True
    return False

def getIdHash(id):
    """  Return md5 prefix based on id value"""
    m = hashlib.new('md5')
    print("id:", id)
    print("type:", type(id))
    m.update(id.encode('utf8'))
    hexdigest = m.hexdigest()
    return hexdigest[:5]

def getS3Key(id):
    """ Return s3 key based on uuid and class char.
    Add a md5 prefix in front of the returned key to better 
    distribute S3 objects"""
    idhash = getIdHash(id)
    key = "{}-{}".format(idhash, id)
    return key

def createId(obj_type):
    if obj_type not in ('group', 'dataset', 'namedtype', 'chunk'):
        raise ValueError("unexpected obj_type")
    id = obj_type[0] + '-' + str(uuid.uuid1())
    return id
    
def getHeadNodeS3Key():
    return "headnode"

def getRootTocUuid():
    """ Return fake uuid of the root TOC group.  This will be the one object that
    can be identified a-priori in a given bucket"""
    zero_uuid = "g-{}-{}-{}-{}-{}".format('0'*8, '0'*4, '0'*4, '0'*4, '0'*12)
    return zero_uuid

def validateUuid(id):
    if not isinstance(id, str):
        raise ValueError("Expected string type")
    if len(id) != 38:  
        # id should be prefix (e.g. "g-") and uuid value
        raise ValueError("Unexpected id length")
    if id[0] not in ('g', 'd', 't', 'c'):
        raise ValueError("Unexpected prefix")
    if id[1] != '-':
        raise ValueError("Unexpected prefix")

def getUuidFromId(id):
    return id[2:]

def getS3Partition(id, count):
    hash_code = getIdHash(id)
    hash_value = int(hash_code, 16)
    number = hash_value % count
    return number
        
    

async def getS3JSONObj(app, id):
    key = getS3Key(id)
    print("getS3JSONObj({})".format(key))
    client = app['s3']
    bucket = app['bucket_name']
    resp = await client.get_object(Bucket=bucket, Key=key)
    data = await resp['Body'].read()
    resp['Body'].close()
    json_dict = json.loads(data.decode('utf8'))
    return json_dict

async def putS3JSONObj(app, id, json_obj):
    key = getS3Key(id)
    print("putS3JSONObj({})".format(key))
    client = app['s3']
    bucket = app['bucket_name']
    data = json.dumps(json_obj)
    data = data.encode('utf8')
    resp = await client.put_object(Bucket=bucket, Key=key, Body=data)
    
     

async def isS3Obj(app, id):
    key = getS3Key(id)
    print("isS3Obj({})".format(key))
    client = app['s3']
    bucket = app['bucket_name']
    resp = await client.list_objects(Bucket=bucket, MaxKeys=1, Prefix=key)
    if 'Contents' not in resp:
        return False
    contents = resp['Contents']
    
    if len(contents) > 0:
        return True
    else:
        return False
    
async def http_get(app, url):
    print("http_get('{}')".format(url))
    client = app['client']
    rsp_json = None
    try:
        async with client.get(url) as rsp:
            print("head response status:", rsp.status)
            rsp_json = await rsp.json()
            print("got response: ", rsp_json)
    except ClientOSError:
        print("unable to connect with", url)
    return rsp_json

async def http_post(app, url, data):
    print("http_post('{}')".format(url))
    client = app['client']
    rsp_json = None
    client = app['client']
    
    async with client.post(url, data=json.dumps(data)) as rsp:
        print("head response status:", rsp.status)
        if isOK(rsp.status):  
            rsp_json = await rsp.json()
            print("got response: ", rsp_json)
    return rsp_json
