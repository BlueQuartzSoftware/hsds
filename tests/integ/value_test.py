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
import helper
 

class ValueTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(ValueTest, self).__init__(*args, **kwargs)
        self.base_domain = helper.getTestDomainName(self.__class__.__name__)
        helper.setupDomain(self.base_domain)
        self.endpoint = helper.getEndpoint()
        
        # main
     
    def testPut1DDataset(self):
        # Test PUT value for 1d dataset

        headers = helper.getRequestHeaders(domain=self.base_domain)
        req = self.endpoint + '/'

        # Get root uuid
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        root_uuid = rspJson["root"]
        helper.validateId(root_uuid)

        # create dataset
        data = { "type": "H5T_STD_I32LE", "shape": 10 }
        
        req = self.endpoint + '/datasets' 
        rsp = requests.post(req, data=json.dumps(data), headers=headers)
        self.assertEqual(rsp.status_code, 201)
        rspJson = json.loads(rsp.text)
        dset_id = rspJson["id"]
        self.assertTrue(helper.validateId(dset_id))

        # link new dataset as 'dset1d'
        name = "dset1d"
        req = self.endpoint + "/groups/" + root_uuid + "/links/" + name 
        payload = {"id": dset_id}
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 201)
        
        # read values from dset (should be zeros)
        req = self.endpoint + "/datasets/" + dset_id + "/value" 
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        self.assertTrue("hrefs" in rspJson)
        self.assertTrue("value" in rspJson)
        self.assertEqual(rspJson["value"], [0,] * data["shape"])

        # write to the dset
        data = list(range(10))  # write 0-9
        payload = { 'value': data }
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 200)
        
        # read back the data
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        self.assertTrue("hrefs" in rspJson)
        self.assertTrue("value" in rspJson)
        self.assertEqual(rspJson["value"], data)

        # read a selection
        params = {"select": "[2:8]"} # read 6 elements, starting at index 2
        rsp = requests.get(req, params=params, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        self.assertTrue("hrefs" in rspJson)
        self.assertTrue("value" in rspJson)
        self.assertEqual(rspJson["value"], list(range(2,8)))


    def testPutSelection1DDataset(self):
        # Test PUT value with selection for 1d dataset

        headers = helper.getRequestHeaders(domain=self.base_domain)
        req = self.endpoint + '/'

        # Get root uuid
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        root_uuid = rspJson["root"]
        helper.validateId(root_uuid)

        # create dataset
        data = { "type": "H5T_STD_I32LE", "shape": 10 }
        
        req = self.endpoint + '/datasets' 
        rsp = requests.post(req, data=json.dumps(data), headers=headers)
        self.assertEqual(rsp.status_code, 201)
        rspJson = json.loads(rsp.text)
        dset_id = rspJson["id"]
        self.assertTrue(helper.validateId(dset_id))

        # link new dataset as 'dset1d'
        name = "dset1d"
        req = self.endpoint + "/groups/" + root_uuid + "/links/" + name 
        payload = {"id": dset_id}
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 201)
        
        # write to dset
        req = self.endpoint + "/datasets/" + dset_id + "/value" 
        data = list(range(10))  # write 0-9
        data_part1 = data[0:5]
        data_part2 = data[5:10]

        # write part 1
        payload = { 'start': 0, 'stop': 5, 'value': data_part1 }
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 200)

        # write part 2
        payload = { 'start': 5, 'stop': 10, 'value': data_part2 }
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 200)
   
        # read back the data
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        self.assertTrue("hrefs" in rspJson)
        self.assertTrue("value" in rspJson)
        self.assertEqual(rspJson["value"], data)

    def testPutSelection2DDataset(self):
        # Test PUT value with selection for 1d dataset

        headers = helper.getRequestHeaders(domain=self.base_domain)
        req = self.endpoint + '/'

        # Get root uuid
        rsp = requests.get(req, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        root_uuid = rspJson["root"]
        helper.validateId(root_uuid)

        # create dataset
        # pass in layout specification so that we can test selection across chunk boundries
        data = { "type": "H5T_STD_I32LE", "shape": [45,54] }
        data['creationProperties'] = {'layout': {'class': 'H5D_CHUNKED', 'dims': [10, 10] }}
        
        req = self.endpoint + '/datasets' 
        rsp = requests.post(req, data=json.dumps(data), headers=headers)
        self.assertEqual(rsp.status_code, 201)
        rspJson = json.loads(rsp.text)
        dset_id = rspJson["id"]
        self.assertTrue(helper.validateId(dset_id))

        # link new dataset as 'dset2d'
        name = "dset2d"
        req = self.endpoint + "/groups/" + root_uuid + "/links/" + name 
        payload = {"id": dset_id}
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 201)
        
        # write a horizontal strip of 22s
        req = self.endpoint + "/datasets/" + dset_id + "/value" 
        data = [22,] * 50
        payload = { 'start': [22, 2], 'stop': [23, 52], 'value': data }
        rsp = requests.put(req, data=json.dumps(payload), headers=headers)
        self.assertEqual(rsp.status_code, 200)

        # read back a vertical strip that crossed the horizontal strip
        params = {"select": "[20:25,21:22]"} # read 6 elements, starting at index 20
        rsp = requests.get(req, params=params, headers=headers)
        self.assertEqual(rsp.status_code, 200)
        rspJson = json.loads(rsp.text)
        self.assertTrue("hrefs" in rspJson)
        self.assertTrue("value" in rspJson)
        value = rspJson["value"]
        self.assertEqual(len(value), 5)
        self.assertEqual(value, [[0,],[0,],[22,],[0,],[0,]])

             
if __name__ == '__main__':
    #setup test files
    
    unittest.main()
    
