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
import unittest
import requests
import json
import config
import helper
 
acl_keys = ('create', 'read', 'update', 'delete', 'readACL', 'updateACL')
class AclTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(AclTest, self).__init__(*args, **kwargs)
        self.base_domain = helper.getTestDomainName(self.__class__.__name__)
        helper.setupDomain(self.base_domain)
        
        # main
     
    def testGetAcl(self):
        print("testGetAcl", self.base_domain)
        headers = helper.getRequestHeaders(domain=self.base_domain)
        
        # there should be an ACL for "test_user1" who has ability to do any action on the domain
        req = helper.getEndpoint() + '/acls/test_user1'
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        self.assertTrue("acl" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acl = rsp_json["acl"]
        self.assertEqual(len(acl.keys()), len(acl_keys))
        for k in acl_keys:
            self.assertTrue(k in acl)
            self.assertTrue(acl[k])

        # get the default ACL.  Only 'read' should be true
        req = helper.getEndpoint() + '/acls/default'
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        self.assertTrue("acl" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acl = rsp_json["acl"]
        self.assertEqual(len(acl.keys()), len(acl_keys))
        for k in acl_keys:
            self.assertTrue(k in acl)
            if k == 'read':
                self.assertEqual(acl[k], True)
            else:
                self.assertEqual(acl[k], False)

        # get the root id
        req = helper.getEndpoint() + '/'
        rsp = requests.get(req, headers=headers)
        rspJson = json.loads(rsp.text)
        self.assertTrue("root" in rspJson)
        root_uuid = rspJson["root"]

        # get the ACL for the Group
        req = helper.getEndpoint() + '/groups/' + root_uuid + "/acls/default"
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        print(rsp_json)
        self.assertTrue("acl" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acl = rsp_json["acl"]
        self.assertEqual(len(acl.keys()), len(acl_keys))
        for k in acl_keys:
            self.assertTrue(k in acl)
            if k == 'read':
                self.assertEqual(acl[k], True)
            else:
                self.assertEqual(acl[k], False)

        # try getting the ACL for a random user, should return 404
        req = helper.getEndpoint() + '/acls/joebob'
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 404)

        # try fetching an ACL from a user who doesn't have readACL permissions
        req = helper.getEndpoint() + '/acls/test_user1'
        headers = helper.getRequestHeaders(domain=self.base_domain, username="test_user2")
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 403) # forbidden

    def testGetAcls(self):
        print("testGetAcls", self.base_domain)
        headers = helper.getRequestHeaders(domain=self.base_domain)
        
        # there should be an ACL for "default" with read-only access and 
        #  "test_user1" who has ability to do any action on the domain
        req = helper.getEndpoint() + '/acls'
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        self.assertTrue("acls" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acls = rsp_json["acls"]
        self.assertEqual(len(acls), 2)
        
        for acl in acls:
            self.assertEqual(len(acl.keys()), len(acl_keys) + 1)
            self.assertTrue('userName' in acl)
            userName = acl['userName']
            self.assertTrue(userName in ("default", "test_user1"))
            if userName == "default":
                for k in acl.keys():
                    if k == "userName":
                        continue
                    if k not in acl_keys:
                        self.assertTrue(False)
                    if k == "read":
                        self.assertEqual(acl[k], True)
                    else:
                        self.assertEqual(acl[k], False)
            else:
                for k in acl.keys():
                    if k == "userName":
                        continue
                    if k not in acl_keys:
                        self.assertTrue(False)
                    self.assertEqual(acl[k], True)
        
        # get root uuid
        req = helper.getEndpoint() + '/'
        rsp = requests.get(req, headers=headers)
        rspJson = json.loads(rsp.text)
        self.assertTrue("root" in rspJson)
        root_uuid = rspJson["root"]

        # get the ACLs for the Group
        req = helper.getEndpoint() + '/groups/' + root_uuid + "/acls"
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        print(rsp_json)
        self.assertTrue("acls" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acls = rsp_json["acls"]
        self.assertEqual(len(acls), 2)


        # create a dataset  
        payload = {'type': 'H5T_STD_I32LE', 'shape': 10,
             'link': {'id': root_uuid, 'name': 'dset'} }
        req = helper.getEndpoint() + "/datasets"
        rsp = requests.post(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 201)  # create dataset
        rspJson = json.loads(rsp.text)
        dset_uuid = rspJson['id']
        self.assertTrue(helper.validateId(dset_uuid))

        # now try getting the ACLs for the dataset
        req = helper.getEndpoint() + '/datasets/' + dset_uuid + "/acls"
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        print(rsp_json)
        self.assertTrue("acls" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acls = rsp_json["acls"]
        self.assertEqual(len(acls), 2)

        # create a committed type
        payload = { 
            'type': 'H5T_IEEE_F64LE', 
            'link': {'id': root_uuid, 'name': 'dtype'} 
        }
         
        req = self.endpoint + "/datatypes"
        # create a new ctype
        rsp = requests.post(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 201) 
        rspJson = json.loads(rsp.text)
        self.assertEqual(rspJson["attributeCount"], 0)
        dtype_uuid = rspJson["id"]
        self.assertTrue(helper.validateId(dtype_uuid) ) 

        # now try getting the ACLs for the datatype
        req = helper.getEndpoint() + '/datatype/' + dtype_uuid + "/acls"
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rsp_json = json.loads(rsp.text)
        self.assertTrue("acls" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acls = rsp_json["acls"]
        self.assertEqual(len(acls), 2)

             
        # try fetching ACLs from a user who doesn't have readACL permissions
        req = helper.getEndpoint() + '/acls'
        headers = helper.getRequestHeaders(domain=self.base_domain, username="test_user2")
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 403) # forbidden



    def testPutAcl(self):
        print("testPutAcl", self.base_domain)
        headers = helper.getRequestHeaders(domain=self.base_domain)

        # there should be an ACL for "test_user2" with read and update access 
        req = helper.getEndpoint() + '/acls/test_user2'
        data = {"read": True, "update": True}
        rsp = requests.put(req, headers=headers, data=json.dumps(data))
        self.assertEqual(rsp.status_code, 201)

        # fetch the acl and verify it has been updated
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rsp_json = json.loads(rsp.text)
        self.assertTrue("acl" in rsp_json)
        self.assertTrue("hrefs" in rsp_json)
        acl = rsp_json["acl"]
        self.assertEqual(len(acl.keys()), len(acl_keys))
        for k in acl_keys:
            self.assertTrue(k in acl)
            if k in ("read", "update"):
                self.assertEqual(acl[k], True)
            else:
                self.assertEqual(acl[k], False)

         

        
        
         


if __name__ == '__main__':
    #setup test files
    
    unittest.main()

