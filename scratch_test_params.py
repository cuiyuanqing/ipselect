import urllib.request, urllib.parse, json, sys

sys.stdout.reconfigure(line_buffering=True)

params = [
    '',
    'status=available',
    'protocol=openvpn',
    'protocol=OpenVPN',
    'proto=udp',
    'proto=tcp',
    'ip_type=residential',
    'quality=proxy'
]

for p in params:
    path = '/api/ui/nodes?country=JP&limit=30' + (('&' + p) if p else '')
    url = 'http://jiakuan.19940407.xyz:8585/api/debug_aimili?path=' + urllib.parse.quote(path)
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=5).read()).get('raw_response', {})
        nodes = data.get('nodes', [])
        protos = set(n.get('protocol') for n in nodes)
        print(f"{p:25}: {len(nodes)} nodes, protos: {protos}")
    except Exception as e:
        print(f"{p:25}: error {e}")
