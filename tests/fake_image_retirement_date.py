#!/usr/bin/env python3
"""Provide one fixed clock to the image-retirement fixture."""
import os
import sys

if sys.argv[1:] == ['-u', '+%s']:
    print('978393600')
else:
    os.execv('/bin/date', ['date', *sys.argv[1:]])
