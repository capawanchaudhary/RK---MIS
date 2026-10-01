PRODUCTION CONTROL & BOM COSTING — RK GROUP

This version is built around the user's actual process.

Operational users enter:
1. Meal quantities at FG/Menu level for the kitchen and service date.
2. Opening Inventory, Purchases and Closing Inventory at Material level.
3. Review the Production Plan to see menu quantities, shared SFG prep, and gross raw-material requirements.

Actual consumption is automatically calculated as:
Actual Consumption = Opening Inventory + Purchases - Closing Inventory

Backend / HO maintains:
- Kitchens and Zones
- FG / Menu Master
- SFG Master
- Material Master
- FG -> SFG BOM
- FG / Menu -> Material recipe, using a recipe portion basis (for example, ingredient quantity for 100 portions)
- SFG -> Material BOM
- Material Rates (global or kitchen-specific)
- Other direct expense rates per meal by service group (e.g. gas, housekeeping allocation)

FG service groups supported exactly:
Breakfast
Lunch & Dinner
Hi-Tea
Extra Paratha
Base Staff Food
TT Staff Food
Train Staff Food

Costing flow:
Meal Demand -> Direct FG Recipe and/or FG -> SFG -> Material BOM -> Ideal Material Requirement -> Material Rate -> FG Cost / Meal

Material requirement formulas:
Direct FG recipe quantity = (Meal Demand / Recipe Portions) x Component Qty
SFG material quantity = (SFG Demand / SFG BOM Base Qty) x Component Qty

FG cost per meal:
FG material cost per meal = SUM(FG BOM SFG Qty per FG x SFG Cost per output unit)
FG total cost per meal = FG material cost per meal + configured direct expense rate for that service group

Dashboard outputs:
1. FG-level per-meal cost
2. Ideal vs Actual consumption

Production Plan outputs:
- Menu quantity by kitchen and service date
- SFG prep quantities, where FG -> SFG BOMs are used
- Material requirements split by service group, with actual usage, quantity variance, and cost variance
- Direct menu-to-material recipes and shared SFG recipes can both be used

No CSV is needed for meal or actual consumption entry.

Running on Windows:
1. Keep Python 3.13+ installed.
2. Extract the folder.
3. Double-click run.bat.
4. Open http://127.0.0.1:8501
5. Use Backend Masters to populate your masters/BOM/rates.
6. Kitchen user uses Meal Entry and Actual Consumption.
7. MD uses RK GROUP.

Online deployment (Vercel + Supabase):
- Vercel serves the app and its Python API; Supabase PostgreSQL stores shared online records.
- Set DATABASE_URL, APP_PASSWORD, and APP_SECRET in Vercel Project Settings → Environment Variables.
- The online app requires one shared sign-in password. Do not reuse an email password.
- To copy the local records, run migrate_to_supabase.bat from the same folder as production_control.db and follow its private prompts. The local database is read only during this copy and is not removed.
- Keep database connection strings and app passwords out of GitHub and chat.

This first online version uses one shared sign-in. Kitchen-specific roles, audit history, and SAP/RISE integration are future improvements for a larger rollout.
