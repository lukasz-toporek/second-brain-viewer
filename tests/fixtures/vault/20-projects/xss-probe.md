---
type: projekt
title: <img src=x onerror=alert(1)>
---
# XSS probe

<img src=x onerror=alert(1)>

<script>window.__pwned = 1</script>

[click me](javascript:alert(1))

[tab](java&#9;script:alert(2))

![pic](javascript:alert(3))

[ok](https://example.com) and [rel](other.md) and [anchor](#xss-probe)
