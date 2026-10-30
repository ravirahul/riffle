"""Compare a rebuilt .py against the original compiled code object.
Usage: python -I bytecheck.py original.marshal rebuilt.py [-v]
Prints each function whose instructions differ (ignores line numbers)."""
import sys, marshal, dis, types

def norm(arg):
    if isinstance(arg, types.CodeType):
        return '<code %s>' % arg.co_qualname
    return repr(arg)

def instrs(co):
    out = []
    for i in dis.get_instructions(co):
        if i.opname in ('NOP', 'RESUME', 'CACHE', 'NOT_TAKEN'):
            continue
        a = i.argval
        if i.opname.startswith(('JUMP', 'POP_JUMP', 'FOR_ITER', 'SEND', 'SETUP')) or 'JUMP' in i.opname:
            a = ''
        out.append('%s %s' % (i.opname, norm(a)))
    return out

def walk(co, acc):
    acc.setdefault(co.co_qualname, []).append(co)
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            walk(c, acc)
    return acc

orig = marshal.loads(open(sys.argv[1], 'rb').read())
new = compile(open(sys.argv[2], encoding='utf-8').read(), sys.argv[2], 'exec')
A, B = walk(orig, {}), walk(new, {})
verbose = '-v' in sys.argv
bad = 0
for name in A:
    if name not in B:
        print('MISSING', name); bad += 1; continue
    for k, (x, y) in enumerate(zip(A[name], B[name])):
        ix, iy = instrs(x), instrs(y)
        if ix != iy:
            bad += 1
            print('DIFF', name, '(%d vs %d instrs)' % (len(ix), len(iy)))
            if verbose:
                import difflib
                for l in list(difflib.unified_diff(ix, iy, 'orig', 'new', lineterm='', n=2))[:40]:
                    print('   ', l)
for name in B:
    if name not in A:
        print('EXTRA', name); bad += 1
print('functions: %d, mismatched: %d' % (len(A), bad))
