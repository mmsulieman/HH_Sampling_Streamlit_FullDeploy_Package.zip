# HH Sampling Streamlit App (Deploy-ready)

✅ **Deploy-ready structure for Streamlit Community Cloud** (main file at repository root: `app.py`).

## What it does
- PPS village selection per kebele (MOS = total HHs)
- Up to 2 villages selected per kebele
- Main sample:
  - Two-village kebele: 15 Eligible + 15 Non-Eligible per selected village
  - One-village kebele: 30 per group
- Reserves:
  - Two-village kebele: 4 per group per selected village
  - One-village kebele: 8 per group
- **Eligible-only rule**: If kebele-wide Non-Eligible total across ALL villages < 30, sample only Eligible
- Rebalancing (main only) within kebele & group
- No overlap between main and reserves
- Reproducible with seed (default 20251031)
- Exports required Excel sheets + optional printable Excel (per kebele) + optional PDF (per woreda)
- Adds `Sample_Code` (system generated)

## Files
- `app.py` (main entrypoint)
- `requirements.txt`
- `runtime.txt` (Python version pin)
- `.streamlit/config.toml` (UI config)

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to Streamlit Community Cloud
1. Push these files to GitHub (repo root)
2. In Streamlit Cloud:
   - **Main file path**: `app.py`
3. Deploy.

## Input columns
Default expects:
- zone, Woreda, Kebele, Village, HH_ID, Eligibility

If different, adjust the JSON mapping in the sidebar.
