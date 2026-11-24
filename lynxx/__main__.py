import sys

if not __package__ and not getattr(sys, 'frozen', False):
    import os.path
    path = os.path.abspath(__file__)
    sys.path.insert(0, os.path.dirname(os.path.dirname(path)))

import lynxx

if __name__ == '__main__':
    lynxx.main(sys.argv[1:])  # exits with lynxx_main's code
