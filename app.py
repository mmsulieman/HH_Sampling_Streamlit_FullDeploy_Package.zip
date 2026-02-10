import streamlit as st
import pandas as pd
import numpy as np
import re
import json
from collections import defaultdict
from io import BytesIO

# Optional PDF export
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

APP_VERSION = "2026-02-08"

st.set_page_config(page_title='HH Sampling (PPS by Village)', layout='wide')

st.title('Household Sampling Tool (PPS by Kebele/Village)')
st.caption(f"Version {APP_VERSION} • Implements PPS village selection (MOS = total HHs), 30/30 split Eligible/Non-eligible where feasible, Eligible-only when kebele Non-eligible total < 30, main-only rebalancing, reserves, no overlap, reproducible seed, exports to Excel and optional PDF.")

# ---------------------------
# Helpers
# ---------------------------

def sanitize_sheet_name(name: str, existing: set) -> str:
    """Excel sheet name must be <=31 chars and cannot contain []:*?/\\"""
    clean = re.sub(r'[\[\]\*\?/\\:]', '_', str(name))
    clean = re.sub(r'\s+', ' ', clean).strip()
    if not clean:
        clean = 'Sheet'
    base = clean[:31]
    cand = base
    i = 1
    while cand in existing:
        suffix = f'_{i}'
        cand = base[:31-len(suffix)] + suffix
        i += 1
    existing.add(cand)
    return cand


def sample_from_pool(pool_idx, n, rng):
    pool_idx = np.array(pool_idx)
    if n <= 0 or len(pool_idx) == 0:
        return np.array([], dtype=int)
    if n >= len(pool_idx):
        return pool_idx
    return rng.choice(pool_idx, size=n, replace=False)


def build_pdf_by_woreda(combined: pd.DataFrame, seed: int) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError('reportlab is not installed; cannot create PDF')

    styles = getSampleStyleSheet()
    pdf_cols = ['Sample_Code','Woreda','Kebele','Village','HH_ID','Household Head Name','Eligibility','Sample_Type']

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=18, rightMargin=18, topMargin=18, bottomMargin=18)

    story = []
    combined = combined.sort_values(['Woreda','Kebele','Village','Sample_Group','Sample_Type','Primary_Order_in_VillageGroup','Reserve_Order_in_VillageGroup'])

    for woreda, wsub in combined.groupby('Woreda'):
        story.append(Paragraph(f"Household Sample List - Woreda: <b>{woreda}</b> (Seed {seed})", styles['Title']))
        story.append(Spacer(1, 8))
        data = [pdf_cols]
        for _, rr in wsub.iterrows():
            data.append([str(rr.get(c,'')) if pd.notna(rr.get(c,'')) else '' for c in pdf_cols])
        tbl = Table(data, repeatRows=1)
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1f4e79')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,0), 9),
            ('FONTSIZE', (0,1), (-1,-1), 7.8),
            ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.whitesmoke, colors.lightgrey])
        ]))
        story.append(tbl)
        story.append(PageBreak())

    if story and isinstance(story[-1], PageBreak):
        story = story[:-1]

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def run_sampling(df: pd.DataFrame, seed: int, col_map: dict):
    """Core sampling engine."""
    rng = np.random.default_rng(seed)

    Z = col_map['zone']; W = col_map['woreda']; K = col_map['kebele']; V = col_map['village']
    HH = col_map['hh_id']; EL = col_map['eligibility']

    required = [Z,W,K,V,HH,EL]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Standardize
    for c in required:
        df[c] = df[c].astype(str).str.strip()

    df['_kebele_key'] = df[Z] + '|' + df[W] + '|' + df[K]

    # De-duplicate HH_ID within kebele
    sort_cols = ['_kebele_key', HH]
    if 'longList_SN' in df.columns:
        sort_cols.append('longList_SN')
    df_frame = (df.sort_values(by=sort_cols, kind='mergesort')
                  .drop_duplicates(subset=['_kebele_key', HH], keep='first')
                  .copy())

    # MOS per village within kebele
    village_mos = df_frame.groupby([Z,W,K,V]).size().reset_index(name='MOS_total_HHs')

    # Kebele group counts (sum across ALL villages)
    kebele_group_counts = (df_frame.groupby([Z,W,K,EL]).size().unstack(fill_value=0).reset_index())
    if 'Eligible' not in kebele_group_counts.columns:
        kebele_group_counts['Eligible'] = 0
    if 'Non-Eligible' not in kebele_group_counts.columns:
        kebele_group_counts['Non-Eligible'] = 0

    # Village selection (up to 2)
    selection_rows = []
    selected_villages_by_kebele = {}
    for (zone, woreda, kebele), sub in village_mos.groupby([Z,W,K]):
        sub = sub.sort_values(V)
        villages = sub[V].tolist()
        mos = sub['MOS_total_HHs'].to_numpy(dtype=float)
        total = mos.sum()
        probs = mos/total if total>0 else np.ones_like(mos)/len(mos)
        n_sel = min(2, len(villages))
        if len(villages) <= 2:
            selected = villages
            method = 'All villages (n<=2)'
        else:
            selected = list(rng.choice(villages, size=n_sel, replace=False, p=probs))
            method = 'PPS w/o replacement'
        selected_villages_by_kebele[(zone,woreda,kebele)] = selected
        for v, m, p in zip(villages, mos, probs):
            selection_rows.append({
                Z: zone, W: woreda, K: kebele, V: v,
                'MOS_total_HHs': int(m),
                'PPS_prob': float(p),
                'Selected': v in selected,
                'Selected_n_villages': n_sel,
                'Selection_method': method
            })
    village_pps_selection = pd.DataFrame(selection_rows)

    # Sampling
    primary_samples, reserve_samples, summary_rows = [], [], []
    kebele_list = village_mos[[Z,W,K]].drop_duplicates().sort_values([Z,W,K])

    for _, row in kebele_list.iterrows():
        zone, woreda, kebele = row[Z], row[W], row[K]
        sel_villages = selected_villages_by_kebele[(zone,woreda,kebele)]
        n_sel = len(sel_villages)

        counts_row = kebele_group_counts[(kebele_group_counts[Z]==zone) & (kebele_group_counts[W]==woreda) & (kebele_group_counts[K]==kebele)].iloc[0]
        non_total = int(counts_row.get('Non-Eligible', 0))

        groups_to_sample = ['Eligible'] if non_total < 30 else ['Eligible','Non-Eligible']
        main_per_village = 30 if n_sel==1 else 15
        reserve_per_village = 8 if n_sel==1 else 4

        for group in groups_to_sample:
            target_main_total = 30
            target_res_total = 8

            pools = {v: df_frame.index[(df_frame[Z]==zone)&(df_frame[W]==woreda)&(df_frame[K]==kebele)&(df_frame[V]==v)&(df_frame[EL]==group)].to_numpy()
                     for v in sel_villages}

            selected_main_by_v = defaultdict(list)
            remaining_by_v = {}
            selected_main = []

            # Initial main draw per village
            for v, idx in pools.items():
                take = min(main_per_village, len(idx))
                chosen = sample_from_pool(idx, take, rng)
                selected_main.extend(chosen.tolist())
                selected_main_by_v[v].extend(chosen.tolist())
                remaining_by_v[v] = np.setdiff1d(idx, chosen, assume_unique=False)

            selected_main = np.array(selected_main, dtype=int)

            # Rebalance main within kebele+group
            shortfall = max(0, target_main_total - len(selected_main))
            if shortfall > 0:
                remaining_all = np.concatenate([remaining_by_v[v] for v in sel_villages]) if n_sel>0 else np.array([], dtype=int)
                add = sample_from_pool(remaining_all, shortfall, rng)
                for i in add.tolist():
                    selected_main_by_v[df_frame.loc[i,V]].append(i)
                selected_main = np.concatenate([selected_main, add])
                for v in sel_villages:
                    remaining_by_v[v] = np.setdiff1d(remaining_by_v[v], add, assume_unique=False)

            # Primary records
            for v in sel_villages:
                ids = selected_main_by_v[v]
                ordered = [ids[i] for i in (rng.permutation(len(ids)) if len(ids) else [])] if len(ids) else []
                for order, idx in enumerate(ordered, start=1):
                    rec = df_frame.loc[idx].to_dict()
                    rec.update({
                        'Sample_Type':'Primary',
                        'Sample_Group':group,
                        'Selected_Villages_in_Kebele':n_sel,
                        'Village_Selected':True,
                        'Primary_Order_in_VillageGroup':order,
                        'Reserve_Order_in_VillageGroup':np.nan,
                        'Random_Seed':seed
                    })
                    primary_samples.append(rec)

            # Reserves (no overlap)
            reserves_by_v = defaultdict(list)
            for v in sel_villages:
                idx_rem = remaining_by_v[v]
                take = min(reserve_per_village, len(idx_rem))
                chosen = sample_from_pool(idx_rem, take, rng)
                reserves_by_v[v].extend(chosen.tolist())
                remaining_by_v[v] = np.setdiff1d(idx_rem, chosen, assume_unique=False)

            for v in sel_villages:
                ids = reserves_by_v[v]
                ordered = [ids[i] for i in (rng.permutation(len(ids)) if len(ids) else [])] if len(ids) else []
                for order, idx in enumerate(ordered, start=1):
                    rec = df_frame.loc[idx].to_dict()
                    rec.update({
                        'Sample_Type':'Reserve',
                        'Sample_Group':group,
                        'Selected_Villages_in_Kebele':n_sel,
                        'Village_Selected':True,
                        'Primary_Order_in_VillageGroup':np.nan,
                        'Reserve_Order_in_VillageGroup':order,
                        'Random_Seed':seed
                    })
                    reserve_samples.append(rec)

            avail_selected = int(sum(len(pools[v]) for v in sel_villages))
            drawn_main = int(len(selected_main))
            drawn_res = int(sum(len(reserves_by_v[v]) for v in sel_villages))
            summary_rows.append({
                Z: zone, W: woreda, K: kebele,
                'Group': group,
                'Villages_Selected': ', '.join(sel_villages),
                'n_Villages_Selected': n_sel,
                'Available_HHs_in_Selected_Villages': avail_selected,
                'NonEligible_Total_in_Kebele_AllVillages': non_total,
                'Target_Main': target_main_total,
                'Drawn_Main': drawn_main,
                'Main_Shortfall': max(0, target_main_total - drawn_main),
                'Target_Reserve': target_res_total,
                'Drawn_Reserve': drawn_res,
                'Reserve_Shortfall': max(0, target_res_total - drawn_res),
                'Eligibility_Sampling_Note': 'Eligible-only (Non-Eligible total in kebele <30)' if non_total<30 else ''
            })

        # add a skipped line for non-eligible if eligible-only
        if non_total < 30:
            summary_rows.append({
                Z: zone, W: woreda, K: kebele,
                'Group': 'Non-Eligible',
                'Villages_Selected': ', '.join(sel_villages),
                'n_Villages_Selected': n_sel,
                'Available_HHs_in_Selected_Villages': int(df_frame[(df_frame[Z]==zone)&(df_frame[W]==woreda)&(df_frame[K]==kebele)&(df_frame[V].isin(sel_villages))&(df_frame[EL]=='Non-Eligible')].shape[0]),
                'NonEligible_Total_in_Kebele_AllVillages': non_total,
                'Target_Main': 0,
                'Drawn_Main': 0,
                'Main_Shortfall': 0,
                'Target_Reserve': 0,
                'Drawn_Reserve': 0,
                'Reserve_Shortfall': 0,
                'Eligibility_Sampling_Note': 'Skipped (Non-Eligible total in kebele <30)'
            })

    kebele_group_summary = pd.DataFrame(summary_rows)

    rollup = (kebele_group_summary.groupby([Z,W,K,'n_Villages_Selected','Villages_Selected'], as_index=False)
              .agg({
                  'Available_HHs_in_Selected_Villages':'sum',
                  'NonEligible_Total_in_Kebele_AllVillages':'max',
                  'Target_Main':'sum',
                  'Drawn_Main':'sum',
                  'Main_Shortfall':'sum',
                  'Target_Reserve':'sum',
                  'Drawn_Reserve':'sum',
                  'Reserve_Shortfall':'sum'
              }))

    primary_df = pd.DataFrame(primary_samples)
    reserve_df = pd.DataFrame(reserve_samples)

    # Sample_Code
    for d in (primary_df, reserve_df, village_pps_selection, kebele_group_summary, rollup):
        d['Kebele_ID'] = d[Z].astype(str) + '|' + d[W].astype(str) + '|' + d[K].astype(str)

    def make_code(row, is_primary=True):
        order = int(row['Primary_Order_in_VillageGroup']) if is_primary else int(row['Reserve_Order_in_VillageGroup'])
        typ = 'P' if is_primary else 'R'
        return f"SYS-{seed}-{row['Kebele_ID']}-{row[V]}-{row['Sample_Group']}-{typ}-{order:03d}"

    primary_df = primary_df.sort_values([Z,W,K,V,'Sample_Group','Primary_Order_in_VillageGroup']).reset_index(drop=True)
    reserve_df = reserve_df.sort_values([Z,W,K,V,'Sample_Group','Reserve_Order_in_VillageGroup']).reset_index(drop=True)

    primary_df['Sample_Code'] = primary_df.apply(lambda r: make_code(r, True), axis=1)
    reserve_df['Sample_Code'] = reserve_df.apply(lambda r: make_code(r, False), axis=1)

    # Column ordering: original df_frame cols (excluding helper) + Sample_Code + meta
    orig_cols = [c for c in df_frame.columns if c != '_kebele_key']
    meta_p = ['Sample_Code'] + [c for c in primary_df.columns if c not in orig_cols and c != 'Sample_Code']
    meta_r = ['Sample_Code'] + [c for c in reserve_df.columns if c not in orig_cols and c != 'Sample_Code']
    primary_df = primary_df[orig_cols + meta_p]
    reserve_df = reserve_df[orig_cols + meta_r]

    return primary_df, reserve_df, village_pps_selection, kebele_group_summary, rollup


# ---------------------------
# UI
# ---------------------------

with st.sidebar:
    st.header('Settings')
    seed = st.number_input('Random seed', min_value=0, value=20251031, step=1)
    make_printable_xlsx = st.checkbox('Create printable Excel (one sheet per kebele)', value=True)
    make_pdf = st.checkbox('Create printable PDF (one sheet per Woreda)', value=False, disabled=not REPORTLAB_AVAILABLE)
    if not REPORTLAB_AVAILABLE:
        st.caption('PDF export disabled (reportlab not installed).')

    st.markdown('---')
    st.subheader('Column map (advanced)')
    default_map = {
        'zone':'zone',
        'woreda':'Woreda',
        'kebele':'Kebele',
        'village':'Village',
        'hh_id':'HH_ID',
        'eligibility':'Eligibility',
        'hh_name':'Household Head Name',
        'gender':'longList_gender'
    }
    col_map_json = st.text_area('JSON mapping', value=json.dumps(default_map, indent=2), height=220)

uploaded = st.file_uploader('Upload Excel HH list (.xlsx)', type=['xlsx'])

if uploaded is None:
    st.info('Upload an Excel file to begin.')
    st.stop()

# Identify sheet names and let user choose
try:
    xls = pd.ExcelFile(uploaded, engine='openpyxl')
    sheet = st.selectbox('Select sheet', options=xls.sheet_names, index=0)
    df_in = pd.read_excel(xls, sheet_name=sheet)
except Exception as e:
    st.error(f'Failed to read Excel: {e}')
    st.stop()

# Parse mapping
try:
    col_map = json.loads(col_map_json)
except Exception as e:
    st.error(f'Invalid JSON mapping: {e}')
    st.stop()

st.success(f'Loaded: {df_in.shape[0]:,} rows • {df_in.shape[1]} cols • sheet: {sheet}')

# Run sampling
try:
    primary_df, reserve_df, village_pps, kebele_group_summary, rollup = run_sampling(df_in, int(seed), col_map)
except Exception as e:
    st.exception(e)
    st.stop()

# KPIs
colZ, colW, colK = col_map['zone'], col_map['woreda'], col_map['kebele']

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric('Primary HHs', f"{len(primary_df):,}")
with c2:
    st.metric('Reserve HHs', f"{len(reserve_df):,}")
with c3:
    st.metric('Kebeles', f"{primary_df[[colZ,colW,colK]].drop_duplicates().shape[0]:,}")
with c4:
    st.metric('Villages selected (rows)', f"{village_pps[village_pps['Selected']].shape[0]:,}")

st.subheader('Quality checks')
overlap = len(set(primary_df.get(col_map['hh_id'], primary_df.get('HH_ID', []))).intersection(set(reserve_df.get(col_map['hh_id'], reserve_df.get('HH_ID', [])))))
if overlap == 0:
    st.success('No overlap between primary and reserves (HH_ID).')
else:
    st.warning(f'Overlap detected: {overlap} HH_IDs appear in both primary and reserves. Check input duplicates.')

with st.expander('Kebele group summary (targets vs achieved)', expanded=True):
    st.dataframe(kebele_group_summary, use_container_width=True)

with st.expander('Primary sample preview (first 50)', expanded=False):
    st.dataframe(primary_df.head(50), use_container_width=True)

with st.expander('Reserve sample preview (first 50)', expanded=False):
    st.dataframe(reserve_df.head(50), use_container_width=True)

# ---------------------------
# Exports (in-memory)
# ---------------------------

out_xlsx = BytesIO()
with pd.ExcelWriter(out_xlsx, engine='openpyxl') as writer:
    primary_df.to_excel(writer, sheet_name='Sampled_HHs_Primary_FullColumns', index=False)
    reserve_df.to_excel(writer, sheet_name='Sampled_HHs_Reserves_FullColumns', index=False)
    village_pps.to_excel(writer, sheet_name='Village_PPS_Selection', index=False)
    kebele_group_summary.to_excel(writer, sheet_name='Kebele_Group_Summary', index=False)
    rollup.to_excel(writer, sheet_name='Kebele_Summary_Rollup', index=False)
out_xlsx.seek(0)

printable_xlsx = None
if make_printable_xlsx:
    # Keep printable list compact
    vcol = col_map['village']
    hhcol = col_map['hh_id']
    namecol = col_map.get('hh_name')
    gencol = col_map.get('gender')
    elcol = col_map.get('eligibility')

    base_cols = ['Sample_Code', colZ, colW, colK, vcol, hhcol]
    for opt in [namecol, gencol, elcol]:
        if opt and opt in primary_df.columns and opt not in base_cols:
            base_cols.append(opt)
    base_cols += ['Sample_Type','Sample_Group','Primary_Order_in_VillageGroup','Reserve_Order_in_VillageGroup']

    buf = BytesIO()
    existing = set()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        for (z,w,k), _ in primary_df.groupby([colZ,colW,colK]):
            kf = primary_df[(primary_df[colZ]==z)&(primary_df[colW]==w)&(primary_df[colK]==k)][base_cols].assign(List='Primary')
            kr = reserve_df[(reserve_df[colZ]==z)&(reserve_df[colW]==w)&(reserve_df[colK]==k)][base_cols].assign(List='Reserve')
            out = pd.concat([kf, kr], ignore_index=True)
            out = out.sort_values([vcol,'Sample_Group','List','Primary_Order_in_VillageGroup','Reserve_Order_in_VillageGroup'])
            out.to_excel(writer, sheet_name=sanitize_sheet_name(str(k), existing), index=False)
    buf.seek(0)
    printable_xlsx = buf.getvalue()

pdf_bytes = None
if make_pdf and REPORTLAB_AVAILABLE:
    # Normalize key columns expected by PDF builder
    def norm(df_):
        rename = {
            col_map['woreda']:'Woreda',
            col_map['kebele']:'Kebele',
            col_map['village']:'Village',
            col_map['hh_id']:'HH_ID',
            col_map['eligibility']:'Eligibility'
        }
        if col_map.get('hh_name'):
            rename[col_map['hh_name']] = 'Household Head Name'
        out = df_.rename(columns=rename).copy()
        if 'Household Head Name' not in out.columns:
            out['Household Head Name'] = ''
        return out

    combined = pd.concat([norm(primary_df), norm(reserve_df)], ignore_index=True)
    pdf_bytes = build_pdf_by_woreda(combined, int(seed))

st.markdown('---')
st.subheader('Downloads')

st.download_button(
    '⬇️ Download main output workbook (Excel)',
    data=out_xlsx.getvalue(),
    file_name=f'HH_Sampling_Output_{seed}.xlsx',
    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
)

if printable_xlsx is not None:
    st.download_button(
        '⬇️ Download printable workbook by kebele (Excel)',
        data=printable_xlsx,
        file_name=f'HH_Sampling_Printable_ByKebele_{seed}.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

if pdf_bytes is not None:
    st.download_button(
        '⬇️ Download printable PDF by Woreda',
        data=pdf_bytes,
        file_name=f'HH_Sampling_Printable_ByWoreda_{seed}.pdf',
        mime='application/pdf'
    )

# Bundle outputs
zip_buf = BytesIO()
import zipfile
with zipfile.ZipFile(zip_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    zf.writestr(f'HH_Sampling_Output_{seed}.xlsx', out_xlsx.getvalue())
    if printable_xlsx is not None:
        zf.writestr(f'HH_Sampling_Printable_ByKebele_{seed}.xlsx', printable_xlsx)
    if pdf_bytes is not None:
        zf.writestr(f'HH_Sampling_Printable_ByWoreda_{seed}.pdf', pdf_bytes)
zip_buf.seek(0)

st.download_button(
    '⬇️ Download ALL outputs (ZIP)',
    data=zip_buf.getvalue(),
    file_name=f'HH_Sampling_Outputs_{seed}.zip',
    mime='application/zip'
)
