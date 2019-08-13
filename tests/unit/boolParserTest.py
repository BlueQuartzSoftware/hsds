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
import time
import sys
import os

sys.path.append('../../hsds/util')
from boolparser import BooleanParser

class BooleanParserTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(BooleanParserTest, self).__init__(*args, **kwargs)
        # main
    

    def testExpressions(self):      

        p = BooleanParser('x1 == "hi" AND y2 > 42') 
        variables = p.getVariables()
        self.assertEqual(len(variables), 2)
        self.assertTrue("x1" in variables)
        self.assertTrue("y2" in variables)
        self.assertTrue(p.evaluate({'x1': 'hi', 'y2': 43}))  


        # usee single instead of double quotes
        p = BooleanParser("x1 == 'hi' AND y2 > 42") 
        variables = p.getVariables()
        
        self.assertEqual(len(variables), 2)
        self.assertTrue("x1" in variables)
        self.assertTrue("y2" in variables)
        self.assertTrue(p.evaluate({'x1': 'hi', 'y2': 43}))  

        p = BooleanParser("x > 2 AND y < 3") 
        self.assertTrue(p.evaluate({'x':3, 'y': 1}))   
        self.assertFalse(p.evaluate({'x':1, 'y': 1}))   

        try:
            p.evaluate({'x':'3', 'y': 1})
            self.assertTrue(false)  # expected exception
        except TypeError:
            pass # expected - type of x is not int

        try:
            p.evaluate({'x': {'a': 1, 'b': 2}, 'y': 1})
            self.assertTrue(false)  # expected exception - dict pased for x value
        except TypeError:
            pass # expected - type of x is not int

        try:
            p.evaluate({'y': 1})
            self.assertTrue(false)  # expected exception
        except TypeError:
            pass # expected - missing 'x' in dict

        try:
            BooleanParser("x > 2 AND")
            self.assertTrue(false)  # expected exception
        except IndexError:
            pass # expected - malformed exception

        try:
            BooleanParser("1 + 1 = 2")
            self.assertTrue(false)  # expected exception
        except Exception:
            pass # expected - malformed exception
                              
             
if __name__ == '__main__':
    #setup test files
    
    unittest.main()
