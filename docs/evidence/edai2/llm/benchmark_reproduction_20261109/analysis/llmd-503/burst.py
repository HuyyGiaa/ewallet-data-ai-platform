import json, sys, urllib.request, concurrent.futures as cf, collections
url = sys.argv[1]; n = int(sys.argv[2])
def one(i):
    req = urllib.request.Request(url, data=json.dumps({"model": "m", "messages": [{"role": "user", "content": (sys.argv[3] if len(sys.argv)>3 else f"hi {i}")}], "max_tokens": 1}).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, ""
    except urllib.error.HTTPError as e: return e.code, e.read()[:200].decode(errors="replace")
    except Exception as e: return "exc", str(e)[:100]
with cf.ThreadPoolExecutor(n) as ex: res = list(ex.map(one, range(n)))
print(collections.Counter(s for s, _ in res)); [print(s, b) for s, b in res if s != 200][:6]
