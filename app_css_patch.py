p = 'web/static/app.css'
s = open(p, encoding='utf-8').read()

# add chart panel styles after the chart .chart rule
anchor = ".chart-legend {"
if anchor:
    pass
else:
    # append before the closing of the file
    s += """
.chart { position: relative; min-height: 280px; background: #0b0f14; border-radius: 10px; overflow: hidden; }
#chart-error { font-size: 12px; }
.chart-legend { display: flex; gap: 10px; font-size: 11px; color: #9ca3af; margin-top: 8px; }
.chart-legend .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.dot-bull { background: #16a34a; }
.dot-bear { background: #dc2626; }

/* symbol search */
.search-box { display: flex; gap: 8px; margin-bottom: 8px; }
.search-box input { flex: 1; padding: 7px 9px; border-radius: 8px; border: 1px solid #374151; background: #0f172a; color: #e5e7eb; font-size: 13px; }
.search-box input:focus { outline: 1px solid #5cf0e6; }
.search-group { display: flex; flex-direction: column; gap: 3px; }
.search-group label { font-size: 11px; color: #9ca3af; }
.search-group select { padding: 6px 8px; border-radius: 6px; border: 1px solid #374151; background: #111827; color: #e5e7eb; }
"""
open(p, 'w', encoding='utf-8').write(s)
print('appended chart styles')
