# vendor/

Static copies of the two front-end libraries, served by `viewer.py --offline` at
`/vendor/<file>` and inlined by `--export`. Refresh or verify with
`python3 tools/fetch-vendor.py` (refuses files whose SHA-256 differs from the pins).

| File | Version | Source | SHA-256 | Licence |
|------|---------|--------|---------|---------|
| `marked.min.js` | marked 9.1.6 | https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js | `6002af63485b043fa60ddaba1b34363b98d2a8b2c63b607004f3a2405a8a053a` | MIT |
| `force-graph.min.js` | force-graph 1.43.5 | https://cdn.jsdelivr.net/npm/force-graph@1.43.5/dist/force-graph.min.js | `af9324ce63bb87f330643c04a9062ca17b3be32826234012197ced595eb0f83a` | MIT |

`force-graph.min.js` is a bundle that also contains small permissively licensed
dependencies (d3-* modules: ISC/BSD-3-Clause, kapsule, etc.).
