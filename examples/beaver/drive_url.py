"""Print a direct download URL for a large public Google Drive file.

Drive interposes a "virus scan warning" page on anything over ~100 MB. The real
download needs the hidden form fields from that page, so curl alone gets HTML.
"""
from __future__ import annotations

import html
import re
import sys
import urllib.parse
import urllib.request

file_id = sys.argv[1]
page = urllib.request.urlopen(
    f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
).read().decode()
action = html.unescape(re.search(r'<form[^>]+action="([^"]+)"', page).group(1))
fields = {
    m.group(1): html.unescape(m.group(2))
    for m in re.finditer(r'name="([^"]+)"\s+value="([^"]*)"', page)
}
print(action + "?" + urllib.parse.urlencode(fields))
