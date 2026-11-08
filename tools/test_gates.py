"""Pro gates in features.post, with every file operation replaced by a recorder.

Nothing on disk is touched: cleanup.recycle / cleanup.delete are swapped for fakes,
and the duplicate groups and cleanup report are made-up paths.
Run: python -I tools/test_gates.py
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import features, cleanup, license

calls = []
cleanup.recycle = lambda paths: calls.append(('recycle', list(paths))) or True
cleanup.delete = lambda paths: calls.append(('delete', list(paths))) or 0
features.os.path.getsize = lambda p: 100          # fake files have a size...
real_exists = os.path.exists
features.os.path.exists = lambda p: p.startswith('Z:\\fake') or real_exists(p)   # ...and "exist"

class H:                                           # minimal stand-in for the HTTP handler
    def _send(self, code, body, ctype=None):
        self.code, self.body = code, json.loads(body)

import copy
GROUPS = {'groups': [
        {'hash': 'a', 'size': 100, 'files': [{'path': 'Z:\\fake\\a1', 'keep': True}, {'path': 'Z:\\fake\\a2', 'keep': False}]},
        {'hash': 'b', 'size': 100, 'files': [{'path': 'Z:\\fake\\b1', 'keep': True}, {'path': 'Z:\\fake\\b2', 'keep': False}]}]}
class Dupes:
    def __init__(self):
        self.state = copy.deepcopy(GROUPS)

class Cleaner:
    card_of = {os.path.normcase('Z:\\fake\\tmp1'): 'temp', os.path.normcase('Z:\\fake\\old.exe'): 'installers'}
    def remove(self, paths):
        calls.append(('clean', list(paths))); return {'freed': 0, 'removed': len(paths), 'refused': 0}
features._cleaner = Cleaner()

d = tempfile.mkdtemp()
features._ctx.update(data_dir=d)
license.init(d)

def post(path, body):
    h = H(); features.post(h, path, body); return h

def tier(t):
    features._dupes = Dupes()                      # fresh made-up scan results each time
    license._state.clear()
    license._state.update(trialStart=(0 if t == 'free' else 10**10), iid='x')

results = {}
tier('free')
calls.clear(); h = post('/api/dupes/remove', {'paths': ['Z:\\fake\\a2', 'Z:\\fake\\b2']})
results['free: bulk across 2 sets -> 402, nothing removed'] = h.code == 402 and not calls
calls.clear(); h = post('/api/dupes/remove', {'paths': ['Z:\\fake\\a2']})
results['free: one set -> allowed'] = h.code == 200 and calls == [('recycle', ['Z:\\fake\\a2'])]
calls.clear(); h = post('/api/cleanup/remove', {'paths': ['Z:\\fake\\old.exe']})
results['free: installers card -> 402, nothing removed'] = h.code == 402 and not calls
calls.clear(); h = post('/api/cleanup/remove', {'paths': ['Z:\\fake\\tmp1']})
results['free: temp card -> allowed'] = h.code == 200 and calls == [('clean', ['Z:\\fake\\tmp1'])]
calls.clear(); h = post('/api/autoclean', {'on': True})
results['free: auto clean-up -> 402'] = h.code == 402
f0 = os.path.join(d, 'old.zip'); open(f0, 'wb').write(b'x' * 10)
features._finding_paths.update(unzipped=[f0], shots=[f0])
calls.clear(); h = post('/api/findings/recycle', {'key': 'unzipped'})
results['free: Worth a look recycle all -> 402, nothing removed'] = h.code == 402 and not calls
tier('trial')
calls.clear(); h = post('/api/findings/recycle', {'key': 'shots'})
results['trial: screenshots never bulk-recycled'] = not calls
calls.clear(); h = post('/api/findings/recycle', {'key': 'unzipped'})
results['trial: Worth a look recycle all -> allowed, counted'] = calls == [('recycle', [f0])] and license.status()['used'].get('freed') == 100
calls.clear(); h = post('/api/dupes/remove', {'paths': ['Z:\\fake\\a2', 'Z:\\fake\\b2']})
results['trial: bulk across sets -> allowed'] = h.code == 200 and len(calls) == 1 and len(calls[0][1]) == 2
calls.clear(); h = post('/api/cleanup/remove', {'paths': ['Z:\\fake\\old.exe']})
results['trial: installers card -> allowed'] = h.code == 200 and calls[0][0] == 'clean'
for k, v in results.items():
    print(('PASS ' if v else 'FAIL ') + k)
sys.exit(0 if all(results.values()) else 1)
