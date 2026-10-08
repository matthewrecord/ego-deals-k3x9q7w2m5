# EGO Deal Watcher

Checks deal feeds every ~30 minutes and pushes a phone notification when an EGO listing
beats your threshold on **net harvesting cost per Ah**:

    (kit price - est. resale of tool/charger) / total Ah     (strong <= $30, watch <= $35)

Runs free on GitHub Actions. No server, nothing to keep alive.

## One-time setup (about 10 minutes)

1. **Phone alerts:** install the free **ntfy** app (iOS/Android). Subscribe to a long random
   topic name, e.g. `ego-deals-k3x9q7w2m5` (anyone who knows the name can read it, so make it unguessable).
2. **GitHub:** create a new repository (private is fine) and upload everything in this folder,
   including the hidden `.github` folder. Easiest: `git init`, `git add .`, commit, push.
3. **Secret:** in the repo go to Settings > Secrets and variables > Actions > New repository secret.
   Name `NTFY_TOPIC`, value = your topic name from step 1.
4. **Test:** Actions tab > "ego-deal-watch" > Run workflow, tick *Send a test notification*.
   Your phone should buzz. (If the Actions tab says workflows are disabled, click to enable them.)

That's it. The schedule starts on its own.

## What keeps it set-and-forget

- **Failure alerts:** if a feed fails 12 runs in a row (~6 h) you get a high-priority alert.
- **Weekly heartbeat:** a silent "alive" ping each week. If you stop seeing it, something is wrong.
  The heartbeat commit also counts as repo activity, which helps keep GitHub from pausing the schedule.
- **Weekly digest:** the weekly ping also lists EGO listings (over $50) it couldn't match to the catalog, so new
  models or matching gaps show up. Add them to `config.yaml` (model regex, Ah, list price).
- **Catalog refresh reminder:** a year after `catalog_checked` in `config.yaml` you get a monthly nudge to re-verify
  list prices and add new models. Update that date after you do.
- **Deep-discount bare tools:** 30 tools that never ship with batteries (misting fans, wet/dry vac, commercial line,
  Multi-Head attachments, etc.) alert as DEEP DISCOUNT when listed 40%+ below their typical new price
  (`bare_tools` in `config.yaml`). Refurbished listings count, but they usually run 30-35% off, so only deeper ones alert.
- **No repeat spam:** each listing alerts once, and again only if its price drops 3%+.

## Maintenance you may want to do occasionally

- `config.yaml` holds 55 kits (mowers, riding mowers, snow blowers, pressure washers, blowers, trimmers,
  chain saws, hedge/pole saws, misc). Each has total Ah, a list price, and sometimes a known bare-tool price.
  Resale is computed as 0.65 x bare-tool price (bare estimated as list price - $28/Ah when unknown), calibrated
  to your HPW3204-2 numbers. Set `resale:` on any kit to override, or tune `resale_factor` globally.
  Prices are approximate Oct 2026 dealer prices; refresh them if EGO reprices.
- Small packs (kits or standalone batteries under 5 Ah total) use a stricter bar, set under
  `small_battery_thresholds` in `config.yaml` (default strong <= $15/Ah, watch <= $20/Ah vs $25/$30 for the rest).
- Reddit frequently blocks cloud IPs. If it fails, delete that source or ignore it.

## Limits to know about

- It only sees what deal feeds surface (Slickdeals, r/ToolDeals). Unadvertised store-clearance
  endcaps won't appear. Price trackers like Keepa could be added later for specific ASINs.
- Titles without a model number are matched by product type + size + total Ah (e.g. 28" snow blower, 2x12Ah ->
  SNT2807). Those alerts are labeled LOW-CONFIDENCE ("verify kit"); bundles ("plus 1 extra battery"), bare-tool
  listings and prices far below list are skipped.
- Titles are parsed with regex: the first `$` amount is treated as the price. Weird titles
  (coupon stacking, "$X off") can mis-parse, so click through before buying.
- GitHub scheduled runs can be delayed during busy periods, so treat timing as approximate.
