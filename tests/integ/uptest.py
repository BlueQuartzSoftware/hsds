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
 
class UpTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(UpTest, self).__init__(*args, **kwargs)
        
        # main
    def testGetInfo(self):
        req = helper.getEndpoint() + '/info'
        rsp = requests.get(req)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.headers['content-type'], 'application/json')
        rspJson = json.loads(rsp.text)
        self.assertTrue("node" in rspJson)
        node = rspJson["node"]
        self.assertTrue("id" in node)
        self.assertTrue(node["id"].startswith("sn-"))
        self.assertTrue("number" in node)
        self.assertTrue(node["number"] >= 0)
        self.assertTrue("type" in node)
        self.assertEqual(node["type"], "sn")
        self.assertTrue("state" in node)
        self.assertEqual(node["state"], "READY")  
             
if __name__ == '__main__':
    #setup test files
    
    unittest.main()
    
