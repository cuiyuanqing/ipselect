import urllib.request, urllib.parse, json

for path in ['/api/nodes?country=JP&limit=5', '/api/nodes?limit=5', '/api/ui/nodes?limit=5']:
    url = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?path=' + urllib.parse.quote(path)
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=5).read()).get('raw_response', {})
        nodes = data.get('nodes', [])
        print(f"{path:30}: {len(nodes)} nodes")
    except Exception as e:
        print(f"{path:30}: error {e}")
