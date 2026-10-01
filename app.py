import json, os, re, sqlite3
from datetime import date, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'production_control.db')
STATIC = os.path.join(BASE, 'static')

SERVICE_GROUPS = [
    'Breakfast', 'Lunch & Dinner', 'Hi-Tea', 'Extra Paratha',
    'Base Staff Food', 'TT Staff Food', 'Train Staff Food'
]

SCHEMA = '''
CREATE TABLE IF NOT EXISTS kitchens(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS materials(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    base_uom TEXT NOT NULL DEFAULT 'KG',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS fgs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    service_group TEXT NOT NULL,
    fg_uom TEXT NOT NULL DEFAULT 'EA',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sfgs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    output_uom TEXT NOT NULL DEFAULT 'KG',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS fg_bom(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fg_code TEXT NOT NULL,
    sfg_code TEXT NOT NULL,
    qty_per_fg REAL NOT NULL,
    uom TEXT NOT NULL DEFAULT 'EA',
    valid_from TEXT NOT NULL DEFAULT '2026-01-01',
    valid_to TEXT NOT NULL DEFAULT '2099-12-31',
    UNIQUE(fg_code,sfg_code,valid_from)
);
CREATE TABLE IF NOT EXISTS fg_material_bom(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fg_code TEXT NOT NULL,
    material_code TEXT NOT NULL,
    base_qty REAL NOT NULL,
    component_qty REAL NOT NULL,
    uom TEXT NOT NULL DEFAULT 'KG',
    valid_from TEXT NOT NULL DEFAULT '2026-01-01',
    valid_to TEXT NOT NULL DEFAULT '2099-12-31',
    UNIQUE(fg_code,material_code,valid_from)
);
CREATE TABLE IF NOT EXISTS sfg_bom(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sfg_code TEXT NOT NULL,
    material_code TEXT NOT NULL,
    base_qty REAL NOT NULL,
    component_qty REAL NOT NULL,
    uom TEXT NOT NULL DEFAULT 'KG',
    valid_from TEXT NOT NULL DEFAULT '2026-01-01',
    valid_to TEXT NOT NULL DEFAULT '2099-12-31',
    UNIQUE(sfg_code,material_code,valid_from)
);
CREATE TABLE IF NOT EXISTS material_rates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_code TEXT NOT NULL,
    kitchen_code TEXT NOT NULL DEFAULT '',
    rate REAL NOT NULL,
    uom TEXT NOT NULL DEFAULT 'KG',
    valid_from TEXT NOT NULL DEFAULT '2026-01-01',
    valid_to TEXT NOT NULL DEFAULT '2099-12-31',
    UNIQUE(material_code,kitchen_code,valid_from)
);
CREATE TABLE IF NOT EXISTS direct_expense_rates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_name TEXT NOT NULL,
    service_group TEXT NOT NULL,
    rate_per_meal REAL NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '2026-01-01',
    valid_to TEXT NOT NULL DEFAULT '2099-12-31',
    UNIQUE(expense_name,service_group,valid_from)
);
CREATE TABLE IF NOT EXISTS meal_demand(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kitchen_code TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    fg_code TEXT NOT NULL,
    meal_qty REAL NOT NULL DEFAULT 0,
    UNIQUE(kitchen_code,plan_date,fg_code)
);
CREATE TABLE IF NOT EXISTS inventory_day(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kitchen_code TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    material_code TEXT NOT NULL,
    opening_qty REAL NOT NULL DEFAULT 0,
    purchase_qty REAL NOT NULL DEFAULT 0,
    closing_qty REAL NOT NULL DEFAULT 0,
    UNIQUE(kitchen_code,plan_date,material_code)
);
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_meal_day ON meal_demand(plan_date,kitchen_code);
CREATE INDEX IF NOT EXISTS idx_inv_day ON inventory_day(plan_date,kitchen_code);
CREATE INDEX IF NOT EXISTS idx_fgbom ON fg_bom(fg_code);
CREATE INDEX IF NOT EXISTS idx_fgm_bom ON fg_material_bom(fg_code);
CREATE INDEX IF NOT EXISTS idx_sfgbom ON sfg_bom(sfg_code);
CREATE INDEX IF NOT EXISTS idx_rates ON material_rates(material_code,kitchen_code);
'''


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    c.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('version','3.0')")
    c.commit(); c.close()


def qrows(sql, params=()):
    c=conn(); out=[dict(r) for r in c.execute(sql,params).fetchall()]; c.close(); return out


def fnum(v):
    try: return float(v or 0)
    except: return 0.0


def clean(v):
    return re.sub(r'\s+',' ',str(v or '').strip())


def dte(v):
    return str(v or date.today().isoformat())[:10]


def active_kitchens():
    return qrows("SELECT code,name,zone FROM kitchens WHERE active=1 ORDER BY name")


def active_fgs():
    return qrows("SELECT code,name,service_group,fg_uom FROM fgs WHERE active=1 ORDER BY service_group,name")


def active_materials():
    return qrows("SELECT code,name,base_uom FROM materials WHERE active=1 ORDER BY name")


def zones():
    return [r['zone'] for r in qrows("SELECT DISTINCT zone FROM kitchens WHERE active=1 AND zone<>'' ORDER BY zone")]


def scoped_kitchens(kitchen='ALL', zone='ALL'):
    where=['active=1']; args=[]
    if kitchen and kitchen!='ALL': where.append('code=?'); args.append(kitchen)
    if zone and zone!='ALL': where.append('zone=?'); args.append(zone)
    return qrows('SELECT code,name,zone FROM kitchens WHERE '+' AND '.join(where)+' ORDER BY name',args)


def lookup_rate(c, material_code, kitchen_code, on_date):
    # Kitchen-specific rate first, then global rate.
    if kitchen_code and kitchen_code!='ALL':
        r=c.execute('''SELECT rate FROM material_rates WHERE material_code=? AND kitchen_code=?
                       AND ? BETWEEN valid_from AND valid_to ORDER BY id DESC LIMIT 1''',
                    (material_code,kitchen_code,on_date)).fetchone()
        if r: return float(r['rate'])
    r=c.execute('''SELECT rate FROM material_rates WHERE material_code=? AND kitchen_code=''
                   AND ? BETWEEN valid_from AND valid_to ORDER BY id DESC LIMIT 1''',
                (material_code,on_date)).fetchone()
    return float(r['rate']) if r else None


def unit_factor(source, target):
    """Return source units per target conversion factor, or None if units are incompatible/unknown."""
    aliases={'EA':'piece','PC':'piece','PCS':'piece','NO':'piece','NOS':'piece','PIECE':'piece','PORTION':'portion',
             'KG':'mass','G':'mass','GM':'mass','GRAM':'mass','GRAMS':'mass','MG':'mass',
             'L':'volume','LTR':'volume','LITRE':'volume','LITER':'volume','ML':'volume'}
    scales={'KG':1000,'G':1,'GM':1,'GRAM':1,'GRAMS':1,'MG':0.001,
            'L':1000,'LTR':1000,'LITRE':1000,'LITER':1000,'ML':1}
    a=clean(source).upper(); b=clean(target).upper()
    if a==b: return 1.0
    if not a or not b or aliases.get(a) is None or aliases.get(a)!=aliases.get(b): return None
    if aliases[a]=='piece' or aliases[a]=='portion': return 1.0
    return scales[a]/scales[b]


def convert_qty(qty, source, target):
    factor=unit_factor(source,target)
    return None if factor is None else float(qty)*factor


def _meal_group_cost_breakdown(kitchen, on_date, service_group):
    """Auditable service-group roll-up; refuses incomplete or incompatible inputs."""
    c=conn(); errors=[]
    demands=c.execute('''SELECT d.fg_code,d.meal_qty,f.name fg_name,f.fg_uom
        FROM meal_demand d JOIN fgs f ON f.code=d.fg_code
        WHERE d.kitchen_code=? AND d.plan_date=? AND f.service_group=? AND d.meal_qty<>0
        ORDER BY f.name''',(kitchen,on_date,service_group)).fetchall()
    if not demands:
        c.close(); return {'on_date':on_date,'kitchen':kitchen,'service_group':service_group,'total_portions':0,'has_demand':False,'ready':True,'errors':[],'sfgs':[],'fg_sfgs':[],'ingredients':[],'expenses':[],'total_value':0,'cost_per_meal':None}
    sfg_boms={}; fg_boms={}; direct_boms={}
    for b in c.execute('SELECT * FROM sfg_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall(): sfg_boms.setdefault(b['sfg_code'],[]).append(b)
    for b in c.execute('SELECT * FROM fg_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall(): fg_boms.setdefault(b['fg_code'],[]).append(b)
    for b in c.execute('SELECT * FROM fg_material_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall(): direct_boms.setdefault(b['fg_code'],[]).append(b)
    sfg_qty={}; fg_links=[]; direct=[]; portions=sum(fnum(d['meal_qty']) for d in demands)
    for d in demands:
        links=fg_boms.get(d['fg_code'],[])
        if not links and not direct_boms.get(d['fg_code']): errors.append(f"{d['fg_name']}: FG BOM or direct material recipe is missing.")
        for b in links:
            info=c.execute('SELECT output_uom,name FROM sfgs WHERE code=?',(b['sfg_code'],)).fetchone()
            output_uom=info['output_uom'] if info else None
            requested=convert_qty(fnum(d['meal_qty'])*fnum(b['qty_per_fg']),b['uom'],output_uom or '')
            if output_uom is None: errors.append(f"{b['sfg_code']}: SFG master/output unit is missing.")
            elif requested is None: errors.append(f"{d['fg_name']} → {b['sfg_code']}: cannot convert {b['uom']} to SFG output unit {output_uom}.")
            else: sfg_qty[b['sfg_code']]=sfg_qty.get(b['sfg_code'],0)+requested
            fg_links.append({'fg_name':d['fg_name'],'meal_qty':fnum(d['meal_qty']),'sfg_code':b['sfg_code'],'sfg_name':info['name'] if info else b['sfg_code'],'qty_per_fg':fnum(b['qty_per_fg']),'link_uom':b['uom'],'total_qty':fnum(d['meal_qty'])*fnum(b['qty_per_fg']),'output_qty':requested,'output_uom':output_uom or ''})
        for b in direct_boms.get(d['fg_code'],[]): direct.append((d,b))
    sfg_rows=[]; ingredient_rows=[]; total_value=0.0
    for code,qty in sfg_qty.items():
        info=c.execute('SELECT name,output_uom FROM sfgs WHERE code=?',(code,)).fetchone(); output=info['output_uom']; rows=[]; value=0.0
        for b in sfg_boms.get(code,[]):
            base=fnum(b['base_qty']);
            if base<=0: errors.append(f"{code} / {b['material_code']}: BOM base quantity must be greater than zero."); continue
            scaled=fnum(b['component_qty'])*qty/base
            rate=c.execute('''SELECT rate,uom FROM material_rates WHERE material_code=? AND kitchen_code IN (?, '') AND ? BETWEEN valid_from AND valid_to ORDER BY CASE WHEN kitchen_code=? THEN 0 ELSE 1 END,id DESC LIMIT 1''',(b['material_code'],kitchen,on_date,kitchen)).fetchone()
            material=c.execute('SELECT name,base_uom FROM materials WHERE code=?',(b['material_code'],)).fetchone()
            if not rate: errors.append(f"{code} / {b['material_code']}: material rate is missing."); rate_uom=''; rate_value=None
            else: rate_uom=rate['uom']; rate_value=float(rate['rate'])
            priced_qty=convert_qty(scaled,b['uom'],rate_uom) if rate_value is not None else None
            if rate_value is not None and priced_qty is None: errors.append(f"{code} / {b['material_code']}: cannot convert BOM unit {b['uom']} to rate unit {rate_uom}.")
            line_value=priced_qty*rate_value if priced_qty is not None and rate_value is not None else None
            if line_value is not None: value+=line_value
            rows.append({'material_code':b['material_code'],'material_name':material['name'] if material else b['material_code'],'base_qty':base,'component_qty':fnum(b['component_qty']),'bom_uom':b['uom'],'scaled_qty':scaled,'rate':rate_value,'rate_uom':rate_uom,'priced_qty':priced_qty,'line_value':line_value})
        if not rows: errors.append(f"{code}: active SFG ingredient BOM is missing.")
        unit_cost=value/qty if qty else 0
        sfg_rows.append({'sfg_code':code,'sfg_name':info['name'],'required_qty':qty,'output_uom':output,'ingredients':rows,'total_value':value,'unit_cost':unit_cost})
        total_value+=value
    direct_rows=[]
    for d,b in direct:
        base=fnum(b['base_qty'])
        if base<=0: errors.append(f"{d['fg_name']} / {b['material_code']}: recipe base quantity must be greater than zero."); continue
        scaled=fnum(d['meal_qty'])*fnum(b['component_qty'])/base
        rate=c.execute('''SELECT rate,uom FROM material_rates WHERE material_code=? AND kitchen_code IN (?, '') AND ? BETWEEN valid_from AND valid_to ORDER BY CASE WHEN kitchen_code=? THEN 0 ELSE 1 END,id DESC LIMIT 1''',(b['material_code'],kitchen,on_date,kitchen)).fetchone()
        priced=convert_qty(scaled,b['uom'],rate['uom']) if rate else None
        if not rate: errors.append(f"{d['fg_name']} / {b['material_code']}: material rate is missing.")
        elif priced is None: errors.append(f"{d['fg_name']} / {b['material_code']}: cannot convert recipe unit {b['uom']} to rate unit {rate['uom']}.")
        line=priced*float(rate['rate']) if priced is not None else None
        material=c.execute('SELECT name FROM materials WHERE code=?',(b['material_code'],)).fetchone()
        direct_rows.append({'fg_name':d['fg_name'],'meal_qty':fnum(d['meal_qty']),'material_code':b['material_code'],'material_name':material['name'] if material else b['material_code'],'base_qty':base,'component_qty':fnum(b['component_qty']),'bom_uom':b['uom'],'scaled_qty':scaled,'priced_qty':priced,'rate':float(rate['rate']) if rate else None,'rate_uom':rate['uom'] if rate else '','line_value':line})
        if line is not None: total_value+=line
    fg_sfg=[]
    for row in fg_links:
        unit=next((x['unit_cost'] for x in sfg_rows if x['sfg_code']==row['sfg_code']),0)
        row['sfg_unit_cost']=unit; row['value']=(row['output_qty'] or 0)*unit; fg_sfg.append(row)
    expenses=[dict(x) for x in c.execute('''SELECT expense_name,rate_per_meal FROM direct_expense_rates WHERE service_group=? AND ? BETWEEN valid_from AND valid_to''',(service_group,on_date)).fetchall()]
    for e in expenses: e['total_value']=fnum(e['rate_per_meal'])*portions; total_value+=e['total_value']
    c.close()
    ready=not errors
    return {'on_date':on_date,'kitchen':kitchen,'service_group':service_group,'total_portions':portions,'has_demand':True,'ready':ready,'errors':errors,'sfgs':sfg_rows,'fg_sfgs':fg_sfg,'ingredients':direct_rows,'expenses':expenses,'total_value':total_value if ready else None,'cost_per_meal':total_value/portions if ready and portions else None}


def meal_cost_breakdown(kitchen, on_date):
    groups=[_meal_group_cost_breakdown(kitchen,on_date,g) for g in SERVICE_GROUPS]
    active=[g for g in groups if g['has_demand']]
    portions=sum(g['total_portions'] for g in active)
    ready=bool(active) and all(g['ready'] for g in active)
    total=sum(g['total_value'] or 0 for g in active) if ready else None
    ld=next((g for g in groups if g['service_group']=='Lunch & Dinner'),None)
    base_rate=ld['cost_per_meal'] if ld and ld['has_demand'] and ld['ready'] else None
    for g in groups:
        g['equivalent_meals']=(g['total_portions']*g['cost_per_meal']/base_rate) if g['has_demand'] and g['ready'] and base_rate else 0
    equivalent=sum(g['equivalent_meals'] for g in groups)
    return {'on_date':on_date,'kitchen':kitchen,'groups':groups,'total_portions':portions,'ready':ready,
            'errors':[f"{g['service_group']}: {err}" for g in active for err in g['errors']]+(["Lunch & Dinner cost per meal is required as the equivalent-meal base."] if active and not base_rate else []),
            'ready':ready and base_rate is not None,'total_value':total,'equivalent_meals':equivalent,
            'cost_per_meal':total/equivalent if ready and equivalent else None,
            'lunch_dinner_portions':ld['total_portions'] if ld else 0}


def compute(kitchen='ALL', on_date=None, zone='ALL'):
    on_date = on_date or date.today().isoformat()
    c=conn(); ks=scoped_kitchens(kitchen,zone); kcodes=[k['code'] for k in ks]
    if not kcodes:
        c.close(); return empty_calc(on_date,kitchen,zone)
    marks=','.join('?'*len(kcodes))

    demand=c.execute(f'''SELECT d.kitchen_code,d.plan_date,d.fg_code,d.meal_qty,
                                f.name fg_name,f.service_group,f.fg_uom
                         FROM meal_demand d JOIN fgs f ON f.code=d.fg_code
                         WHERE d.plan_date=? AND d.kitchen_code IN ({marks}) AND d.meal_qty<>0''',
                    (on_date,*kcodes)).fetchall()

    fg_bom_cache={}; fg_material_bom_cache={}; sfg_bom_cache={}
    for r in c.execute('SELECT * FROM fg_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall():
        fg_bom_cache.setdefault(r['fg_code'],[]).append(r)
    for r in c.execute('SELECT * FROM fg_material_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall():
        fg_material_bom_cache.setdefault(r['fg_code'],[]).append(r)
    for r in c.execute('SELECT * FROM sfg_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall():
        sfg_bom_cache.setdefault(r['sfg_code'],[]).append(r)

    sfg_demand={}; sfg_group={}; missing_fg_bom=set(); missing_sfg_bom=set(); missing_rates=set(); invalid_cost_inputs=set()
    for d in demand:
        links=fg_bom_cache.get(d['fg_code'],[])
        if not links and not fg_material_bom_cache.get(d['fg_code']): missing_fg_bom.add(d['fg_code'])
        for b in links:
            qty=fnum(d['meal_qty'])*fnum(b['qty_per_fg'])
            sfg_info=c.execute('SELECT output_uom FROM sfgs WHERE code=?',(b['sfg_code'],)).fetchone()
            normalized_qty=convert_qty(qty,b['uom'],sfg_info['output_uom']) if sfg_info else None
            if normalized_qty is None:
                invalid_cost_inputs.add(f"{d['fg_code']} / {b['sfg_code']} unit conversion")
            else: qty=normalized_qty
            sfg_demand[b['sfg_code']]=sfg_demand.get(b['sfg_code'],0)+qty
            sg=d['service_group'] or 'Breakfast'
            sfg_group[(b['sfg_code'],sg)]=sfg_group.get((b['sfg_code'],sg),0)+qty

    sfg_unit_cost={}; material_rows={}
    # Per-SFG unit cost. For ALL-kitchen view, global rate is used unless only one kitchen is selected.
    rate_kitchen = kcodes[0] if len(kcodes)==1 else 'ALL'
    for sfg_code,sfg_qty in sfg_demand.items():
        links=sfg_bom_cache.get(sfg_code,[])
        if not links: missing_sfg_bom.add(sfg_code)
        total=0.0
        for b in links:
            base=fnum(b['base_qty']); comp=fnum(b['component_qty'])
            if base<=0: invalid_cost_inputs.add(f"{sfg_code} / {b['material_code']} BOM base")
            req=(sfg_qty/base*comp) if base else 0.0
            rate=lookup_rate(c,b['material_code'],rate_kitchen,on_date)
            if rate is None: missing_rates.add(b['material_code'])
            rate_row=c.execute('''SELECT uom FROM material_rates WHERE material_code=? AND kitchen_code IN (?, '') AND ? BETWEEN valid_from AND valid_to ORDER BY CASE WHEN kitchen_code=? THEN 0 ELSE 1 END,id DESC LIMIT 1''',(b['material_code'],rate_kitchen,on_date,rate_kitchen)).fetchone()
            if rate_row and unit_factor(b['uom'],rate_row['uom']) is None: invalid_cost_inputs.add(f"{sfg_code} / {b['material_code']} unit conversion")
            priced_req=convert_qty(req,b['uom'],rate_row['uom']) if rate_row else None
            if rate_row and priced_req is None: invalid_cost_inputs.add(f"{sfg_code} / {b['material_code']} unit conversion")
            cost=(priced_req or 0)*(rate or 0); total+=cost
            mr=material_rows.setdefault(b['material_code'],{'material_code':b['material_code'],'material_name':'','uom':b['uom'],'ideal_qty':0.0,'ideal_cost':0.0,'by_group':{}})
            mrow=c.execute('SELECT name FROM materials WHERE code=?',(b['material_code'],)).fetchone()
            mr['material_name']=mrow['name'] if mrow else b['material_code']
            mr['ideal_qty']+=req; mr['ideal_cost']+=cost
            for (sfgg,sg),sg_qty in sfg_group.items():
                if sfgg!=sfg_code: continue
                group_req=(sg_qty/base*comp) if base else 0.0
                mr['by_group'][sg]=mr['by_group'].get(sg,0)+group_req
        sfg_unit_cost[sfg_code]=total/sfg_qty if sfg_qty else 0.0

    direct_fg_unit_cost={}
    for d in demand:
        fg_code=d['fg_code']; kitchen_code=d['kitchen_code']; qty=fnum(d['meal_qty'])
        key=(fg_code,kitchen_code)
        if key not in direct_fg_unit_cost:
            direct_cost=0.0
            for recipe in fg_material_bom_cache.get(fg_code,[]):
                base=fnum(recipe['base_qty']); rate=lookup_rate(c,recipe['material_code'],kitchen_code,on_date)
                if base<=0: invalid_cost_inputs.add(f"{fg_code} / {recipe['material_code']} recipe base")
                if rate is None: missing_rates.add(recipe['material_code'])
                rate_row=c.execute('''SELECT uom FROM material_rates WHERE material_code=? AND kitchen_code IN (?, '') AND ? BETWEEN valid_from AND valid_to ORDER BY CASE WHEN kitchen_code=? THEN 0 ELSE 1 END,id DESC LIMIT 1''',(recipe['material_code'],kitchen_code,on_date,kitchen_code)).fetchone()
                if rate_row and unit_factor(recipe['uom'],rate_row['uom']) is None: invalid_cost_inputs.add(f"{fg_code} / {recipe['material_code']} unit conversion")
                if base:
                    recipe_qty=fnum(recipe['component_qty'])/base
                    rate_row=c.execute('''SELECT uom FROM material_rates WHERE material_code=? AND kitchen_code IN (?, '') AND ? BETWEEN valid_from AND valid_to ORDER BY CASE WHEN kitchen_code=? THEN 0 ELSE 1 END,id DESC LIMIT 1''',(recipe['material_code'],kitchen_code,on_date,kitchen_code)).fetchone()
                    priced=convert_qty(recipe_qty,recipe['uom'],rate_row['uom']) if rate_row else None
                    direct_cost+=(priced or 0)*(rate or 0.0)
            direct_fg_unit_cost[key]=direct_cost
        for b in fg_material_bom_cache.get(fg_code,[]):
            base=fnum(b['base_qty']); component=fnum(b['component_qty'])
            per_meal=component/base if base else 0.0
            required=qty*per_meal
            rate=lookup_rate(c,b['material_code'],rate_kitchen,on_date)
            if rate is None: missing_rates.add(b['material_code'])
            rate_row=c.execute('''SELECT uom FROM material_rates WHERE material_code=? AND kitchen_code IN (?, '') AND ? BETWEEN valid_from AND valid_to ORDER BY CASE WHEN kitchen_code=? THEN 0 ELSE 1 END,id DESC LIMIT 1''',(b['material_code'],rate_kitchen,on_date,rate_kitchen)).fetchone()
            if base<=0: invalid_cost_inputs.add(f"{fg_code} / {b['material_code']} recipe base")
            if rate_row and unit_factor(b['uom'],rate_row['uom']) is None: invalid_cost_inputs.add(f"{fg_code} / {b['material_code']} unit conversion")
            mr=material_rows.setdefault(b['material_code'],{'material_code':b['material_code'],'material_name':'','uom':b['uom'],'ideal_qty':0.0,'ideal_cost':0.0,'by_group':{}})
            mrow=c.execute('SELECT name FROM materials WHERE code=?',(b['material_code'],)).fetchone()
            mr['material_name']=mrow['name'] if mrow else b['material_code']
            priced_required=convert_qty(required,b['uom'],rate_row['uom']) if rate_row else None
            if rate_row and priced_required is None: invalid_cost_inputs.add(f"{fg_code} / {b['material_code']} unit conversion")
            material_cost=(priced_required or 0)*(rate or 0.0)
            mr['ideal_qty']+=required; mr['ideal_cost']+=material_cost
            sg=d['service_group'] or 'Breakfast'
            mr['by_group'][sg]=mr['by_group'].get(sg,0.0)+required

    direct_by_group={g:0.0 for g in SERVICE_GROUPS}
    for r in c.execute('''SELECT service_group,SUM(rate_per_meal) rate FROM direct_expense_rates
                           WHERE ? BETWEEN valid_from AND valid_to GROUP BY service_group''',(on_date,)).fetchall():
        direct_by_group[r['service_group']]=fnum(r['rate'])

    fg_cost=[]; groups={g:{'service_group':g,'meal_qty':0.0,'sfg_material_cost':0.0,'direct_expense_cost':0.0,'total_cost':0.0,'cost_per_meal':0.0} for g in SERVICE_GROUPS}
    for d in demand:
        sg=d['service_group'] or 'Breakfast'; links=fg_bom_cache.get(d['fg_code'],[])
        sfg_per_meal=sum(fnum(b['qty_per_fg'])*sfg_unit_cost.get(b['sfg_code'],0) for b in links)+direct_fg_unit_cost.get((d['fg_code'],d['kitchen_code']),0.0)
        direct=direct_by_group.get(sg,0); total_pm=sfg_per_meal+direct; tq=total_pm*fnum(d['meal_qty'])
        row={'kitchen_code':d['kitchen_code'],'service_group':sg,'fg_code':d['fg_code'],'fg_name':d['fg_name'],'meal_qty':fnum(d['meal_qty']),
             'sfg_material_cost_per_meal':sfg_per_meal,'direct_expense_per_meal':direct,'total_cost_per_meal':total_pm,'total_cost':tq}
        fg_cost.append(row); g=groups[sg]; g['meal_qty']+=row['meal_qty']; g['sfg_material_cost']+=sfg_per_meal*row['meal_qty']; g['direct_expense_cost']+=direct*row['meal_qty']; g['total_cost']+=tq
    for g in groups.values(): g['cost_per_meal']=g['total_cost']/g['meal_qty'] if g['meal_qty'] else 0.0
    base_meal_cost=groups['Lunch & Dinner']['cost_per_meal']
    equivalent_meals=(sum(g['meal_qty']*g['cost_per_meal']/base_meal_cost for g in groups.values())
                      if base_meal_cost>0 else 0.0)
    costing_ready=not (missing_fg_bom or missing_sfg_bom or missing_rates or invalid_cost_inputs)
    if not costing_ready:
        for row in fg_cost:
            row['sfg_material_cost_per_meal']=None; row['direct_expense_per_meal']=None
            row['total_cost_per_meal']=None; row['total_cost']=None
        for group_row in groups.values():
            group_row['cost_per_meal']=None; group_row['total_cost']=None

    inv=c.execute(f'''SELECT i.material_code,m.name material_name,m.base_uom,
                             SUM(i.opening_qty) opening_qty,SUM(i.purchase_qty) purchase_qty,SUM(i.closing_qty) closing_qty
                      FROM inventory_day i JOIN materials m ON m.code=i.material_code
                      WHERE i.plan_date=? AND i.kitchen_code IN ({marks}) GROUP BY i.material_code''',(on_date,*kcodes)).fetchall()
    actual_map={r['material_code']:{'name':r['material_name'],'uom':r['base_uom'],'actual_qty':fnum(r['opening_qty'])+fnum(r['purchase_qty'])-fnum(r['closing_qty'])} for r in inv}

    materials=[]
    for code,m in material_rows.items():
        a=actual_map.get(code,{'actual_qty':0,'name':m['material_name'],'uom':m['uom']})
        rate=lookup_rate(c,code,rate_kitchen,on_date); av=a['actual_qty']*(rate or 0)
        materials.append({'material_code':code,'material_name':m['material_name'],'uom':m['uom'],'ideal_qty':m['ideal_qty'],'actual_qty':a['actual_qty'],
                          'qty_variance':a['actual_qty']-m['ideal_qty'],'rate':rate,'ideal_cost':m['ideal_cost'],'actual_cost':av,
                          'cost_variance':av-m['ideal_cost'],'by_group':m['by_group']})
    for code,a in actual_map.items():
        if code not in material_rows:
            rate=lookup_rate(c,code,rate_kitchen,on_date); av=a['actual_qty']*(rate or 0)
            materials.append({'material_code':code,'material_name':a['name'],'uom':a['uom'],'ideal_qty':0,'actual_qty':a['actual_qty'],
                              'qty_variance':a['actual_qty'],'rate':rate,'ideal_cost':0,'actual_cost':av,'cost_variance':av,'by_group':{}})

    total_meals=sum(r['meal_qty'] for r in demand); ideal=sum(r['total_cost'] for r in fg_cost) if costing_ready else None; actual=sum(r['actual_cost'] for r in materials)
    if not costing_ready:
        for row in materials: row['ideal_cost']=None; row['cost_variance']=None
    c.close()
    return {'on_date':on_date,'kitchen':kitchen,'zone':zone,'kitchens':ks,'service_groups':SERVICE_GROUPS,'group_summary':list(groups.values()),
            'fg_cost':fg_cost,'materials':materials,'total_meals':total_meals,'equivalent_meals':equivalent_meals,
            'ideal_cost':ideal,'ideal_cost_per_meal':ideal/equivalent_meals if costing_ready and equivalent_meals else None,
            'costing_ready':costing_ready,'invalid_cost_inputs':sorted(invalid_cost_inputs),
            'actual_cost':actual,'actual_cost_per_meal':actual/total_meals if total_meals else 0,'cost_variance':actual-ideal if ideal is not None else None,
            'missing_fg_bom':sorted(missing_fg_bom),'missing_sfg_bom':sorted(missing_sfg_bom),'missing_rates':sorted(missing_rates)}


def empty_calc(on_date,kitchen,zone):
    return {'on_date':on_date,'kitchen':kitchen,'zone':zone,'kitchens':[],'service_groups':SERVICE_GROUPS,
            'group_summary':[{'service_group':g,'meal_qty':0,'sfg_material_cost':0,'direct_expense_cost':0,'total_cost':0,'cost_per_meal':0} for g in SERVICE_GROUPS],
            'fg_cost':[],'materials':[],'total_meals':0,'equivalent_meals':0,'ideal_cost':0,'ideal_cost_per_meal':None,'actual_cost':0,'actual_cost_per_meal':0,'cost_variance':0,
            'missing_fg_bom':[],'missing_sfg_bom':[],'missing_rates':[]}


def compute_period(kitchen='ALL', on_date=None, zone='ALL', period_start=None):
    """Roll the overview up across inclusive dates, then convert group portions to Lunch & Dinner equivalents."""
    end=date.fromisoformat(on_date or date.today().isoformat())
    start=date.fromisoformat(period_start or end.isoformat())
    if start>end: raise ValueError('Period start must be on or before the date/period end.')
    daily=[]; day=start
    while day<=end:
        daily.append(compute(kitchen,day.isoformat(),zone)); day+=timedelta(days=1)
    groups={g:{'service_group':g,'meal_qty':0.0,'ideal_value':0.0,'cost_per_meal':0.0,'equivalent_meals':0.0} for g in SERVICE_GROUPS}
    fgs={}; materials={}; missing_fg=set(); missing_sfg=set(); missing_rates=set(); invalid=set()
    costing_ready=all(x['costing_ready'] for x in daily)
    for day_data in daily:
        missing_fg.update(day_data['missing_fg_bom']); missing_sfg.update(day_data['missing_sfg_bom']); missing_rates.update(day_data['missing_rates']); invalid.update(day_data['invalid_cost_inputs'])
        for gr in day_data['group_summary']:
            out=groups[gr['service_group']]; out['meal_qty']+=gr['meal_qty']
            if day_data['costing_ready']: out['ideal_value']+=gr['total_cost']
        for row in day_data['fg_cost']:
            key=(row['service_group'],row['fg_code']); out=fgs.setdefault(key,{**row,'meal_qty':0.0,'total_cost':0.0})
            out['meal_qty']+=row['meal_qty']
            if day_data['costing_ready']: out['total_cost']+=row['total_cost']
        for row in day_data['materials']:
            out=materials.setdefault(row['material_code'],{**row,'ideal_qty':0.0,'actual_qty':0.0,'ideal_cost':0.0,'actual_cost':0.0,'by_group':{}})
            out['ideal_qty']+=row['ideal_qty']; out['actual_qty']+=row['actual_qty']; out['actual_cost']+=row['actual_cost']
            if day_data['costing_ready']: out['ideal_cost']+=row['ideal_cost']
            for group,qty0 in row['by_group'].items(): out['by_group'][group]=out['by_group'].get(group,0.0)+qty0
    for row in fgs.values():
        row['total_cost']=row['total_cost'] if costing_ready else None
        row['total_cost_per_meal']=row['total_cost']/row['meal_qty'] if costing_ready and row['meal_qty'] else None
    for row in materials.values():
        row['qty_variance']=row['actual_qty']-row['ideal_qty']
        row['cost_variance']=row['ideal_cost']-row['actual_cost'] if costing_ready else None
        if not costing_ready: row['ideal_cost']=None
    for gr in groups.values(): gr['cost_per_meal']=gr['ideal_value']/gr['meal_qty'] if costing_ready and gr['meal_qty'] else None
    base=groups['Lunch & Dinner']['cost_per_meal']
    equivalent=sum((g['meal_qty']*g['cost_per_meal']/base) for g in groups.values() if g['meal_qty'] and g['cost_per_meal'] is not None) if costing_ready and base else 0.0
    ideal=sum(g['ideal_value'] for g in groups.values()) if costing_ready else None
    actual=sum(x['actual_cost'] for x in materials.values())
    for g in groups.values(): g['equivalent_meals']=(g['meal_qty']*g['cost_per_meal']/base) if g['meal_qty'] and g['cost_per_meal'] is not None and base else None
    total_meals=sum(g['meal_qty'] for g in groups.values())
    return {'on_date':end.isoformat(),'period_start':start.isoformat(),'period_end':end.isoformat(),'kitchen':kitchen,'zone':zone,
        'kitchens':daily[-1]['kitchens'] if daily else [],'service_groups':SERVICE_GROUPS,'group_summary':list(groups.values()),
        'fg_cost':list(fgs.values()),'materials':list(materials.values()),'total_meals':total_meals,'equivalent_meals':equivalent,
        'ideal_cost':ideal,'ideal_cost_per_meal':ideal/equivalent if ideal is not None and equivalent else None,
        'actual_cost':actual,'actual_cost_per_meal':actual/equivalent if equivalent else None,
        'cost_variance':ideal-actual if ideal is not None else None,'costing_ready':costing_ready,
        'missing_fg_bom':sorted(missing_fg),'missing_sfg_bom':sorted(missing_sfg),'missing_rates':sorted(missing_rates),
        'invalid_cost_inputs':sorted(invalid)}


def production_plan(kitchen='ALL', on_date=None, zone='ALL'):
    """Explode a kitchen/day's meal plan through the active FG and SFG BOMs."""
    on_date = on_date or date.today().isoformat()
    c=conn(); ks=scoped_kitchens(kitchen,zone); codes=[k['code'] for k in ks]
    if not codes:
        c.close(); return {'on_date':on_date,'kitchens':[],'fgs':[],'sfgs':[],'materials':[],
                           'missing_fg_bom':[],'missing_sfg_bom':[]}
    marks=','.join('?'*len(codes))
    demands=c.execute(f'''SELECT d.kitchen_code,d.fg_code,d.meal_qty,f.name fg_name,f.fg_uom,f.service_group
                          FROM meal_demand d JOIN fgs f ON f.code=d.fg_code
                          WHERE d.plan_date=? AND d.kitchen_code IN ({marks}) AND d.meal_qty<>0
                          ORDER BY d.kitchen_code,f.service_group,f.name''',(on_date,*codes)).fetchall()
    fg_boms={}; fg_material_boms={}; sfg_boms={}
    for b in c.execute('SELECT * FROM fg_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall():
        fg_boms.setdefault(b['fg_code'],[]).append(b)
    for b in c.execute('SELECT * FROM fg_material_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall():
        fg_material_boms.setdefault(b['fg_code'],[]).append(b)
    for b in c.execute('SELECT * FROM sfg_bom WHERE ? BETWEEN valid_from AND valid_to',(on_date,)).fetchall():
        sfg_boms.setdefault(b['sfg_code'],[]).append(b)
    fg_rows=[]; sfg_rows={}; mat_rows={}; missing_fg=set(); missing_sfg=set(); invalid_recipes=set()
    group_rows={g:{'service_group':g,'meal_qty':0.0,'ideal_material_value':0.0,'cost_per_meal':0.0} for g in SERVICE_GROUPS}
    for d in demands:
        qty=fnum(d['meal_qty']); links=fg_boms.get(d['fg_code'],[])
        group=d['service_group'] or 'Breakfast'
        group_rows.setdefault(group,{'service_group':group,'meal_qty':0.0,'ideal_material_value':0.0,'cost_per_meal':0.0})
        group_rows[group]['meal_qty']+=qty
        fg_rows.append({'kitchen_code':d['kitchen_code'],'fg_code':d['fg_code'],'fg_name':d['fg_name'],
                        'service_group':d['service_group'],'meal_qty':qty,'uom':d['fg_uom']})
        if not links and not fg_material_boms.get(d['fg_code']): missing_fg.add(d['fg_code'])
        for b in fg_material_boms.get(d['fg_code'],[]):
            portions=fnum(b['base_qty'])
            if portions<=0:
                invalid_recipes.add(f"{d['fg_code']} / {b['material_code']}"); continue
            material_qty=qty/portions*fnum(b['component_qty'])
            mr=mat_rows.setdefault((d['kitchen_code'],b['material_code']),{'kitchen_code':d['kitchen_code'],
                'material_code':b['material_code'],'material_name':'','required_qty':0.0,'uom':b['uom'],'by_group':{}})
            mr['required_qty']+=material_qty
            mr['by_group'][group]=mr['by_group'].get(group,0.0)+material_qty
        for b in links:
            req=qty*fnum(b['qty_per_fg'])
            key=(d['kitchen_code'],b['sfg_code'])
            sr=sfg_rows.setdefault(key,{'kitchen_code':d['kitchen_code'],'sfg_code':b['sfg_code'],'sfg_name':'',
                                         'required_qty':0.0,'uom':'','source_fgs':set()})
            sr['required_qty']+=req; sr['source_fgs'].add(d['fg_code'])
            if not sfg_boms.get(b['sfg_code']): missing_sfg.add(b['sfg_code'])
            for x in sfg_boms.get(b['sfg_code'],[]):
                base=fnum(x['base_qty'])
                if base<=0:
                    invalid_recipes.add(f"{b['sfg_code']} / {x['material_code']}"); continue
                material_qty=req/base*fnum(x['component_qty'])
                mr=mat_rows.setdefault((d['kitchen_code'],x['material_code']),{'kitchen_code':d['kitchen_code'],
                    'material_code':x['material_code'],'material_name':'','required_qty':0.0,'uom':x['uom'],'by_group':{}})
                mr['required_qty']+=material_qty
                mr['by_group'][group]=mr['by_group'].get(group,0.0)+material_qty
    for sr in sfg_rows.values():
        row=c.execute('SELECT name,output_uom FROM sfgs WHERE code=?',(sr['sfg_code'],)).fetchone()
        sr['sfg_name']=row['name'] if row else sr['sfg_code']; sr['uom']=row['output_uom'] if row else 'KG'
        sr['source_fgs']=', '.join(sorted(sr['source_fgs']))
    actual_rows=c.execute(f'''SELECT i.kitchen_code,i.material_code,m.name material_name,m.base_uom,
                                    i.opening_qty+i.purchase_qty-i.closing_qty actual_qty
                             FROM inventory_day i JOIN materials m ON m.code=i.material_code
                             WHERE i.plan_date=? AND i.kitchen_code IN ({marks})''',(on_date,*codes)).fetchall()
    for a in actual_rows:
        key=(a['kitchen_code'],a['material_code'])
        mr=mat_rows.setdefault(key,{'kitchen_code':a['kitchen_code'],'material_code':a['material_code'],
            'material_name':a['material_name'],'required_qty':0.0,'uom':a['base_uom'],'by_group':{}})
        mr['actual_qty']=fnum(a['actual_qty'])
    for mr in mat_rows.values():
        if not mr['material_name']:
            row=c.execute('SELECT name FROM materials WHERE code=?',(mr['material_code'],)).fetchone()
            mr['material_name']=row['name'] if row else mr['material_code']
        rate=lookup_rate(c,mr['material_code'],mr['kitchen_code'],on_date)
        mr['rate']=rate; mr['ideal_value']=mr['required_qty']*(rate or 0.0)
        mr['actual_qty']=fnum(mr.get('actual_qty',0)); mr['qty_variance']=mr['actual_qty']-mr['required_qty']
        mr['actual_value']=mr['actual_qty']*(rate or 0.0); mr['value_variance']=mr['actual_value']-mr['ideal_value']
        for group,group_qty in mr['by_group'].items():
            group_rows.setdefault(group,{'service_group':group,'meal_qty':0.0,'ideal_material_value':0.0,'cost_per_meal':0.0})
            group_rows[group]['ideal_material_value']+=group_qty*(rate or 0.0)
    for g in group_rows.values():
        g['cost_per_meal']=g['ideal_material_value']/g['meal_qty'] if g['meal_qty'] else 0.0
    c.close()
    return {'on_date':on_date,'kitchens':ks,'fgs':fg_rows,'sfgs':list(sfg_rows.values()),
            'materials':list(mat_rows.values()),'missing_fg_bom':sorted(missing_fg),
            'missing_sfg_bom':sorted(missing_sfg),'service_groups':list(group_rows.values()),
            'invalid_recipes':sorted(invalid_recipes)}


def meal_entry(k,d):
    c=conn(); existing={r['fg_code']:r['meal_qty'] for r in c.execute('SELECT fg_code,meal_qty FROM meal_demand WHERE kitchen_code=? AND plan_date=?',(k,d)).fetchall()}; c.close()
    return [dict(x,meal_qty=fnum(existing.get(x['code'],0))) for x in active_fgs()]


def actual_entry(k,d):
    c=conn(); existing={r['material_code']:dict(r) for r in c.execute('SELECT * FROM inventory_day WHERE kitchen_code=? AND plan_date=?',(k,d)).fetchall()}
    prev=(date.fromisoformat(d)-timedelta(days=1)).isoformat()
    prev_close={r['material_code']:fnum(r['closing_qty']) for r in c.execute('SELECT material_code,closing_qty FROM inventory_day WHERE kitchen_code=? AND plan_date=?',(k,prev)).fetchall()}; c.close()
    out=[]
    for m in active_materials():
        x=existing.get(m['code'],{}); op=fnum(x.get('opening_qty',prev_close.get(m['code'],0))); pu=fnum(x.get('purchase_qty',0)); cl=fnum(x.get('closing_qty',0))
        out.append({'code':m['code'],'name':m['name'],'uom':m['base_uom'],'opening_qty':op,'purchase_qty':pu,'closing_qty':cl,'actual_qty':op+pu-cl})
    return out


def save_meals(p):
    k=clean(p.get('kitchen_code')); d=dte(p.get('plan_date')); rows=p.get('rows') or []
    if not k: raise ValueError('Kitchen is required')
    c=conn(); c.execute('DELETE FROM meal_demand WHERE kitchen_code=? AND plan_date=?',(k,d)); n=0
    for r in rows:
        fg=clean(r.get('fg_code')); q=fnum(r.get('meal_qty'))
        if fg and q: c.execute('INSERT INTO meal_demand(kitchen_code,plan_date,fg_code,meal_qty) VALUES(?,?,?,?)',(k,d,fg,q)); n+=1
    c.commit(); c.close(); return n


def save_actuals(p):
    k=clean(p.get('kitchen_code')); d=dte(p.get('plan_date')); rows=p.get('rows') or []
    if not k: raise ValueError('Kitchen is required')
    c=conn(); c.execute('DELETE FROM inventory_day WHERE kitchen_code=? AND plan_date=?',(k,d)); n=0
    for r in rows:
        m=clean(r.get('material_code')); op=fnum(r.get('opening_qty')); pu=fnum(r.get('purchase_qty')); cl=fnum(r.get('closing_qty'))
        if m:
            c.execute('INSERT INTO inventory_day(kitchen_code,plan_date,material_code,opening_qty,purchase_qty,closing_qty) VALUES(?,?,?,?,?,?)',(k,d,m,op,pu,cl)); n+=1
    c.commit(); c.close(); return n


def master_rows(entity):
    sqls={'kitchens':'SELECT * FROM kitchens ORDER BY name','materials':'SELECT * FROM materials ORDER BY name','fgs':'SELECT * FROM fgs ORDER BY service_group,name',
          'sfgs':'SELECT * FROM sfgs ORDER BY name','fg_bom':'SELECT * FROM fg_bom ORDER BY fg_code,sfg_code','fg_material_bom':'SELECT * FROM fg_material_bom ORDER BY fg_code,material_code','sfg_bom':'SELECT * FROM sfg_bom ORDER BY sfg_code,material_code',
          'material_rates':'SELECT * FROM material_rates ORDER BY material_code,kitchen_code','direct_expense_rates':'SELECT * FROM direct_expense_rates ORDER BY service_group,expense_name'}
    if entity not in sqls: raise ValueError('Unknown master'); return qrows(sqls[entity])
    return qrows(sqls[entity])


def master_add(entity,d):
    specs={
      'kitchens':(['code','name','zone','active'],'kitchens'),'materials':(['code','name','base_uom','active'],'materials'),
      'fgs':(['code','name','service_group','fg_uom','active'],'fgs'),'sfgs':(['code','name','output_uom','active'],'sfgs'),
      'fg_bom':(['fg_code','sfg_code','qty_per_fg','uom','valid_from','valid_to'],'fg_bom'),
      'fg_material_bom':(['fg_code','material_code','base_qty','component_qty','uom','valid_from','valid_to'],'fg_material_bom'),
      'sfg_bom':(['sfg_code','material_code','base_qty','component_qty','uom','valid_from','valid_to'],'sfg_bom'),
      'material_rates':(['material_code','kitchen_code','rate','uom','valid_from','valid_to'],'material_rates'),
      'direct_expense_rates':(['expense_name','service_group','rate_per_meal','valid_from','valid_to'],'direct_expense_rates')}
    if entity not in specs: raise ValueError('Unknown master')
    cols,table=specs[entity]; vals=[]
    for col in cols:
        v=d.get(col,'')
        if col=='active': v=1 if str(v).lower() in ('1','true','yes','y') else 0
        elif col in ('qty_per_fg','base_qty','component_qty','rate','rate_per_meal'): v=fnum(v)
        elif col in ('valid_from','valid_to'): v=dte(v)
        vals.append(v)
    c=conn(); ph=','.join('?'*len(cols)); c.execute(f'INSERT OR REPLACE INTO {table}({",".join(cols)}) VALUES({ph})',vals); rid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close(); return rid


def master_update(entity,rid,d):
    specs={
      'kitchens':(['code','name','zone','active'],'kitchens'),'materials':(['code','name','base_uom','active'],'materials'),
      'fgs':(['code','name','service_group','fg_uom','active'],'fgs'),'sfgs':(['code','name','output_uom','active'],'sfgs'),
      'fg_bom':(['fg_code','sfg_code','qty_per_fg','uom','valid_from','valid_to'],'fg_bom'),
      'fg_material_bom':(['fg_code','material_code','base_qty','component_qty','uom','valid_from','valid_to'],'fg_material_bom'),
      'sfg_bom':(['sfg_code','material_code','base_qty','component_qty','uom','valid_from','valid_to'],'sfg_bom'),
      'material_rates':(['material_code','kitchen_code','rate','uom','valid_from','valid_to'],'material_rates'),
      'direct_expense_rates':(['expense_name','service_group','rate_per_meal','valid_from','valid_to'],'direct_expense_rates')}
    if entity not in specs: raise ValueError('Unknown master')
    cols,table=specs[entity]
    if entity in ('kitchens','fgs') and (not clean(d.get('code')) or not clean(d.get('name'))):
        raise ValueError('Code and Name are required')
    if entity in ('fgs','direct_expense_rates') and clean(d.get('service_group')) not in SERVICE_GROUPS:
        raise ValueError('Choose one of the seven listed service groups')
    vals=[]
    for col in cols:
        v=d.get(col,'')
        if col=='active': v=1 if str(v).lower() in ('1','true','yes','y') else 0
        elif col in ('qty_per_fg','base_qty','component_qty','rate','rate_per_meal'): v=fnum(v)
        elif col in ('valid_from','valid_to'): v=dte(v)
        vals.append(v)
    c=conn(); c.execute(f'UPDATE {table} SET '+','.join(f'{col}=?' for col in cols)+' WHERE id=?',(*vals,int(rid)))
    if c.total_changes==0: c.close(); raise ValueError('Record not found')
    c.commit(); c.close()


def master_delete(entity,rid):
    if entity not in {'kitchens','materials','fgs','sfgs','fg_bom','fg_material_bom','sfg_bom','material_rates','direct_expense_rates'}: raise ValueError('Unknown master')
    c=conn(); c.execute(f'DELETE FROM {entity} WHERE id=?',(int(rid),)); c.commit(); c.close()


def stats():
    c=conn(); out={}
    for t in ['kitchens','materials','fgs','sfgs','fg_bom','fg_material_bom','sfg_bom','material_rates','direct_expense_rates','meal_demand','inventory_day']:
        out[t]=c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    c.close(); return out

class Handler(BaseHTTPRequestHandler):
    server_version='ProductionControlV3'
    def send_json(self,obj,status=200):
        b=json.dumps(obj,ensure_ascii=False,default=str).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def send_file(self,path,ctype):
        with open(path,'rb') as f: b=f.read(); self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path); qs=parse_qs(p.query)
        try:
            if p.path=='/': return self.send_file(os.path.join(STATIC,'index.html'),'text/html; charset=utf-8')
            if p.path=='/app.js': return self.send_file(os.path.join(STATIC,'app.js'),'application/javascript; charset=utf-8')
            if p.path=='/styles.css': return self.send_file(os.path.join(STATIC,'styles.css'),'text/css; charset=utf-8')
            if p.path=='/api/health': return self.send_json({'ok':True,'version':'3.0'})
            if p.path=='/api/meta': return self.send_json({'service_groups':SERVICE_GROUPS,'kitchens':active_kitchens(),'zones':zones(),'fgs':active_fgs(),'materials':active_materials()})
            if p.path=='/api/dashboard': return self.send_json(compute_period(qs.get('kitchen',['ALL'])[0],qs.get('date',[date.today().isoformat()])[0],qs.get('zone',['ALL'])[0],qs.get('period_start',[qs.get('date',[date.today().isoformat()])[0]])[0]))
            if p.path=='/api/production_plan': return self.send_json(production_plan(qs.get('kitchen',['ALL'])[0],qs.get('date',[date.today().isoformat()])[0],qs.get('zone',['ALL'])[0]))
            if p.path=='/api/meal_cost_breakdown': return self.send_json(meal_cost_breakdown(qs.get('kitchen',[''])[0],qs.get('date',[date.today().isoformat()])[0]))
            if p.path=='/api/meal_entry': return self.send_json(meal_entry(qs.get('kitchen',[''])[0],qs.get('date',[date.today().isoformat()])[0]))
            if p.path=='/api/actual_entry': return self.send_json(actual_entry(qs.get('kitchen',[''])[0],qs.get('date',[date.today().isoformat()])[0]))
            if p.path=='/api/master': return self.send_json(master_rows(qs.get('entity',[''])[0]))
            if p.path=='/api/stats': return self.send_json(stats())
            return self.send_json({'error':'Not found'},404)
        except Exception as e: return self.send_json({'error':str(e)},500)
    def do_POST(self):
        try:
            n=int(self.headers.get('Content-Length','0')); obj=json.loads(self.rfile.read(n).decode('utf-8') or '{}')
            if self.path=='/api/save_meals': return self.send_json({'ok':True,'rows_saved':save_meals(obj)})
            if self.path=='/api/save_actuals': return self.send_json({'ok':True,'rows_saved':save_actuals(obj)})
            if self.path=='/api/master_add': return self.send_json({'ok':True,'id':master_add(obj['entity'],obj.get('data') or {})})
            if self.path=='/api/master_update': master_update(obj['entity'],obj['id'],obj.get('data') or {}); return self.send_json({'ok':True})
            if self.path=='/api/master_delete': master_delete(obj['entity'],obj['id']); return self.send_json({'ok':True})
            return self.send_json({'error':'Not found'},404)
        except Exception as e: return self.send_json({'error':str(e)},500)

if __name__=='__main__':
    init_db(); host=os.environ.get('HOST','127.0.0.1'); port=int(os.environ.get('PORT','8501'))
    print(f'Production Control V3 running at http://{host}:{port}')
    ThreadingHTTPServer((host,port),Handler).serve_forever()
