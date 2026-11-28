import sys

if not __package__ and not getattr(sys, 'frozen', False):
    import os.path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lynxx

if __name__ == '__main__':
    lynxx.main(sys.argv[1:])
