import os

# test runs never record their fake downloads in the user's download index (lynxx/dlindex.py): test_dlindex turns it on
# itself, with a temporary data dir
os.environ.setdefault('LYNXX_DLINDEX', '0')
# nor learn tag lists from the fake sites' posts (lynxx/tags_learned.py): test_tag_sources turns it on itself
os.environ.setdefault('LYNXX_TAGS_LEARN', '0')
# Lynxx ships no shared rx key and refuses to run rx without one: the offline tests (fake sites) use a dummy key
os.environ.setdefault('LYNXX_API_KEY_RX', 'offline-test-key-0000,0')
