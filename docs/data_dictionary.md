# HINO SafePilot Data Dictionary

## Scope and evidence levels

This dictionary describes the private workbook without reproducing any telemetry row or identifier value.

- **Organizer-confirmed**: stated in `hino_field_faq.pdf`, `itraq_hackathon_manual.pdf`, or the workbook's `欄位對照表` sheet.
- **Observed**: derived by read-only inspection of the `output` sheet.
- **Proposed target dtype/name**: an ETL design, not a claim about the raw cell type.
- **UNRESOLVED**: evidence is absent, inconsistent, or requires a product/domain decision.

Source abbreviations: `FAQ Qn` refers to `hino_field_faq.pdf`; `Manual pn` refers to `itraq_hackathon_manual.pdf`; `Mapping rn` refers to workbook sheet `欄位對照表`, row `n` as displayed by Excel/openpyxl.

## Journey identity - organizer confirmed

`journeyCode` is generated independently by each vehicle device. It encodes year/month/day/hour in UTC+0 plus a four-digit sequence, but it contains no vehicle identity and is not globally unique (FAQ Q3). `enabledCode` is the unique vehicle identifier (FAQ Q4). Therefore one vehicle journey is uniquely identified by:

```text
(enabledCode, journeyCode)
```

This is not inferred from the column names; FAQ Q3 explicitly instructs using both fields. The audit also observed many `journeyCode` values shared by multiple vehicles, consistent with the FAQ.

## Base telemetry fields

| Source sheet | Source column | Normalized name | Definition | Observed / proposed dtype | Nullable | ML relevance | Source of definition | Notes | Unresolved issue |
|---|---|---|---|---|---|---|---|---|---|
| `output` | `Type` | `vehicle_series_code` | HINO series code: 300, 500, or 700. | raw int; target categorical int16 | No | slice/context candidate; not a direct safety signal | FAQ Q2 | Three documented values observed. | Whether series may be a model feature versus evaluation slice only. |
| `output` | `journeyCode` | `journey_code` | Per-device journey number: UTC+0 YYMMDDHH plus four-digit sequence. | raw str; target string ID | No | grouping/split only | Mapping r2; FAQ Q3 | Mapping calls it number, but raw cells are strings; preserve as ID. | Relationship between encoded hour and `time` timezone. |
| `output` | `time` | `recorded_at` | Telemetry observation time, format `yyyy-mm-dd hh:mm:ss`. | raw str; target timestamp | No | event-time axis | Mapping r3; Manual p4 | All rows parsed in audit; timezone offset absent. | Timezone and daylight-saving interpretation. |
| `output` | `carStatus` | `vehicle_status` | 0 parking, 1 driving, 2 idling. | raw str; target categorical int8 | No | feature/exposure filter | Mapping r4 | All observed values fit documented domain. | Exact transition semantics at ignition boundaries. |
| `output` | `enabledCode` | `vehicle_id` | Unique vehicle identifier / management code. | raw str; target string ID | No | grouping, vehicle holdout; never direct feature | Mapping r5; FAQ Q4 | All observed values have documented 10-character length. Confidential identifier. | None for journey identity. |
| `output` | `carNum` | `license_plate` | Vehicle registration number. | raw str; target string ID | Yes | audit/display only; exclude from ML | Mapping r7 | Missing in a small portion; each vehicle's nonblank value was internally consistent. Confidential identifier. | Why early/missing records lack the value. |
| `output` | `gps.longitude` | `longitude_deg` | Longitude, documented range -180 to 180. | raw str; target float64 | Yes | optional GPS/context; privacy-sensitive | Mapping r8 | A small number of rows are missing; observed nonblank values fit range. | Whether raw coordinates are permitted as ML features. |
| `output` | `gps.latitude` | `latitude_deg` | Latitude, documented range -90 to 90. | raw str; target float64 | Yes | optional GPS/context; privacy-sensitive | Mapping r9 | Missing pattern matches longitude; observed nonblank values fit range. | Whether raw coordinates are permitted as ML features. |
| `output` | `gps.speed` | `gps_speed_kph` | GPS speed, documented 0.0-255.9 KM/H. | raw str; target float32 | No | core motion feature | Mapping r10 | Two observed values exceed the documented range; max observed is 477. | Outlier cause and quarantine/correction policy. |
| `output` | `gps.azimuth` | `gps_azimuth_deg` | Heading angle, documented 0.0-359.9 degrees. | raw str; target float32 | No | motion feature | Mapping r11 | 61 rows contain 360.0, outside the documented maximum. | Whether 360 should wrap to 0 or be quarantined. |
| `output` | `gps.speedLimit` | `gps_speed_limit` | Road-segment speed limit. | raw str; target float32 | No | candidate overspeed feature | Mapping r12 | Zero is present and may mean unavailable rather than a true limit. | Unit and zero/missing semantics are `UNRESOLVED`. |
| `output` | `can.canStatus` | `can_status` | CAN status: 0 normal, 9 abnormal. | raw int; target categorical int8 | No | quality flag/feature gate | Mapping r13 | Both documented values observed; no other domain values. | Whether abnormal rows should be excluded or handled by a reduced model. |
| `output` | `can.totalMileage` | `total_mileage_km` | Cumulative vehicle mileage in km. | raw str; target float64 | No | past delta/quality feature | Mapping r14 | Within documented range. Cumulative signals require reset/monotonic checks per vehicle. | Reset/replacement semantics. |
| `output` | `can.canSpeed` | `can_speed_kph` | CAN-reported speed in km/h. | raw str; target float32 | No | core motion feature | Mapping r15 | Within documented range. | Sensor preference when GPS and CAN disagree. |
| `output` | `line.fuelLevel` | `fuel_level_pct` | Fuel level percentage, 0-100%. | raw str; target float32 | No | fuel/quality feature | Mapping r21 | Within documented range; discrete steps observed. | Sensor resolution and reliability for trip consumption. |
| `output` | `line.battery` | `battery_mv` | Battery voltage value in mV, documented 0-30,000. | raw str; target float32 | No | vehicle-state candidate | Mapping r22 | Within documented range. | Whether ignition state changes expected interpretation. |
| `output` | `can.engine.engineCoolantTemp` | `engine_coolant_temp_c` | Engine coolant temperature, -40 to 210 °C. | raw str; target float32 | No | engine-state feature | Mapping r16 | Within documented range; -40 may be a sentinel or valid lower bound. | Sentinel semantics. |
| `output` | `can.engine.totalFuelUsed` | `total_fuel_used_l` | Cumulative high-precision fuel used in liters. | raw str; target float64 | No | past delta/fuel metrics | Mapping r17 | Within documented range. | Reset/rollover/replacement semantics. |
| `output` | `can.engine.engineTotalTime` | `engine_total_time_h` | Cumulative engine operating time in hours. | raw str; target float64 | No | past delta/quality feature | Mapping r18 | Within documented range. | Reset/rollover semantics. |
| `output` | `can.engine.rpm` | `engine_rpm` | Engine revolutions per minute. | raw str; target float32 | No | core engine feature | Mapping r19 | Within documented range. | None. |
| `output` | `can.engine.engineLoad` | `engine_load_pct` | Current engine load percentage; mapping states range 0-250. | raw str; target float32 | No | core engine feature | Mapping r20 | Observed range is 0-100; event load mapping separately states 0-100. | Meaning of documented 101-250 range and whether value is truly percent. |
| `output` | `fenceId` | `fence_ids` | Fleet-manager-defined geofence IDs, `&`-delimited when present. | structurally blank; target list[string] | Yes (100%) | unavailable in current ML | Mapping r6; FAQ Q1 | FAQ confirms all competition vehicles have no geofence, so all values are blank. | None; do not impute or invent. |

## Flattened event fields

The raw sheet provides at most three flattened event slots. A slot is a storage position, not a class. Organizer event IDs are: 1 driving, 2 idle, 3 parked, 4 engine on, 5 fence, 6 accelerate, 7 decelerate, 8 speeding, 9 DTC, 10 engine overheat, 11 engine overloading, 12 fatigue, 13 seatbelt, 14 phone, 15 smoking, 16 distract, 17 lens ward-off, 18 driver lost, 19 PCS, and 20 LDWS (Mapping r24-r82).

All event fields are nullable because they apply only when that slot contains a compatible event. `startTime` is an onset candidate; `endTime`, `duration`, and other completed-event details are label/audit data and are not available as online features at the prediction cutoff.

| Source sheet | Source column | Normalized name | Definition | Observed / proposed dtype | Nullable | ML relevance | Source of definition | Notes | Unresolved issue |
|---|---|---|---|---|---|---|---|---|---|
| `output` | `event[0].type` | `event_0_type` | Organizer event ID in first flattened slot. | raw str; target categorical int8 | Yes | label source; past onset history only after lag | Mapping r23-r82 | First slot contains most observed events; slot order has no documented severity meaning. | Maximum-event/truncation and slot ordering semantics. |
| `output` | `event[0].startTime` | `event_0_started_at` | Event start time. | raw str; target timestamp | Yes | label onset source | Mapping event-specific `startTime` rows | Often equals row time; sometimes precedes it, indicating an active/repeated event. | Event deduplication identity beyond type + start time. |
| `output` | `event[0].endTime` | `event_0_ended_at` | End time for event types that have an end. | raw str; target timestamp | Yes | label/audit only; future-leaking feature | Mapping r28/r36/r47/r57/r62 | Not defined for instantaneous event types. | Whether end information is backfilled into earlier rows. |
| `output` | `event[0].info.duration` | `event_0_duration_s` | Duration in seconds for idle/fence/speeding/overheat/overload events. | raw int; target int32 | Yes | label severity/audit only | Mapping r29/r37/r48/r58/r63 | Event-type-dependent. | Operational availability time and sustained-speeding threshold. |
| `output` | `event[0].info.speedDifference` | `event_0_speed_difference` | Speed difference for accelerate/decelerate events. | raw str; target float32 | Yes | label severity/audit only | Mapping r41/r44 | Unit is not stated in mapping. | Unit and sign convention. |
| `output` | `event[0].info.speedLimit` | `event_0_speed_limit_kph` | Speed limit attached to speeding event. | raw str; target float32 | Yes | label audit only | Mapping r49 | Mapping states km/H. | Whether it has same source/semantics as `gps.speedLimit`. |
| `output` | `event[0].info.speed` | `event_0_speed_kph` | Vehicle speed attached to speeding event. | raw str; target float32 | Yes | label audit only | Mapping r50 | Mapping states km/H. | Which speed sensor supplies it. |
| `output` | `event[0].info.dtcCount` | `event_0_dtc_count` | Number of DTC codes for a DTC event. | raw str; target int16 | Yes | quality/maintenance label candidate | Mapping r53 | Event-type-dependent. | None. |
| `output` | `event[0].info.waterTemp` | `event_0_water_temp_c` | Water/engine temperature for overheat event. | all blank; target float32 | Yes (100%) | unavailable | Mapping r59 | Event type 10 was not observed. | Whether field is populated in other exports. |
| `output` | `event[0].info.engineLoad` | `event_0_engine_load_pct` | Engine load attached to overload event. | raw str; target float32 | Yes | label severity/audit only | Mapping r64 | Mapping range 0-100%. | Difference from base engine-load range 0-250. |
| `output` | `event[1].type` | `event_1_type` | Organizer event ID in second flattened slot. | raw str; target categorical int8 | Yes | label source; past onset history only after lag | Mapping r23-r82 | Much sparser than slot 0. | Maximum-event/truncation and slot ordering semantics. |
| `output` | `event[1].startTime` | `event_1_started_at` | Event start time. | raw str; target timestamp | Yes | label onset source | Mapping event-specific `startTime` rows | Same semantics as slot 0. | Event deduplication identity. |
| `output` | `event[1].endTime` | `event_1_ended_at` | Event end time when applicable. | raw str; target timestamp | Yes | label/audit only; future-leaking feature | Mapping r28/r36/r47/r57/r62 | Structurally inapplicable for instant events. | Operational availability time. |
| `output` | `event[1].info.duration` | `event_1_duration_s` | Event duration in seconds when applicable. | raw int; target int32 | Yes | label severity/audit only | Mapping r29/r37/r48/r58/r63 | Event-type-dependent. | Sustained-speeding operational threshold. |
| `output` | `event[1].info.speedDifference` | `event_1_speed_difference` | Accelerate/decelerate speed difference. | all blank; target float32 | Yes (100%) | unavailable in this slot/export | Mapping r41/r44 | Mapping allows it conceptually but audit found no values in slot 1. | Whether absence is structural or sample-specific. |
| `output` | `event[1].info.speedLimit` | `event_1_speed_limit_kph` | Speeding-event limit. | raw str; target float32 | Yes | label audit only | Mapping r49 | Sparse. | Relationship to GPS limit. |
| `output` | `event[1].info.speed` | `event_1_speed_kph` | Speeding-event vehicle speed. | raw str; target float32 | Yes | label audit only | Mapping r50 | Sparse. | Sensor source. |
| `output` | `event[1].info.dtcCount` | `event_1_dtc_count` | DTC count. | raw str; target int16 | Yes | quality/maintenance label candidate | Mapping r53 | Extremely sparse. | None. |
| `output` | `event[1].info.waterTemp` | `event_1_water_temp_c` | Overheat-event water temperature. | all blank; target float32 | Yes (100%) | unavailable | Mapping r59 | Event type 10 not observed. | Whether populated elsewhere. |
| `output` | `event[1].info.engineLoad` | `event_1_engine_load_pct` | Overload-event engine load. | raw str; target float32 | Yes | label severity/audit only | Mapping r64 | Sparse. | Difference from base range. |
| `output` | `event[2].type` | `event_2_type` | Organizer event ID in third flattened slot. | raw str; target categorical int8 | Yes | label source; past onset history only after lag | Mapping r23-r82 | Very sparse. | Whether more than three simultaneous events are truncated. |
| `output` | `event[2].startTime` | `event_2_started_at` | Event start time. | raw str; target timestamp | Yes | label onset source | Mapping event-specific `startTime` rows | Same semantics as other slots. | Event deduplication identity. |
| `output` | `event[2].endTime` | `event_2_ended_at` | Event end time when applicable. | raw str; target timestamp | Yes | label/audit only; future-leaking feature | Mapping r28/r36/r47/r57/r62 | Extremely sparse. | Operational availability time. |
| `output` | `event[2].info.duration` | `event_2_duration_s` | Event duration in seconds when applicable. | raw int; target int32 | Yes | label severity/audit only | Mapping r29/r37/r48/r58/r63 | Extremely sparse. | Sustained-speeding operational threshold. |
| `output` | `event[2].info.speedDifference` | `event_2_speed_difference` | Accelerate/decelerate speed difference. | raw str; target float32 | Yes | label severity/audit only | Mapping r41/r44 | Extremely sparse; unit unspecified. | Unit and sign convention. |
| `output` | `event[2].info.speedLimit` | `event_2_speed_limit_kph` | Speeding-event limit. | raw str; target float32 | Yes | label audit only | Mapping r49 | Extremely sparse. | Relationship to GPS limit. |
| `output` | `event[2].info.speed` | `event_2_speed_kph` | Speeding-event vehicle speed. | raw str; target float32 | Yes | label audit only | Mapping r50 | Extremely sparse. | Sensor source. |
| `output` | `event[2].info.dtcCount` | `event_2_dtc_count` | DTC count. | raw str; target int16 | Yes | quality/maintenance label candidate | Mapping r53 | Extremely sparse. | None. |
| `output` | `event[2].info.waterTemp` | `event_2_water_temp_c` | Overheat-event water temperature. | all blank; target float32 | Yes (100%) | unavailable | Mapping r59 | Event type 10 not observed. | Whether populated elsewhere. |
| `output` | `event[2].info.engineLoad` | `event_2_engine_load_pct` | Overload-event engine load. | raw str; target float32 | Yes | label severity/audit only | Mapping r64 | Extremely sparse. | Difference from base range. |
| `output` | `event[0].info.dtcCodes[0]` | `event_0_dtc_code_0` | First exported DTC code for slot 0. | raw str; target string | Yes | audit/maintenance only | Mapping r54 | Mapping describes an `&`-delimited array named `dtcCode`; output exposes only index 0. | Whether additional DTC codes were truncated. |
| `output` | `event[1].info.dtcCodes[0]` | `event_1_dtc_code_0` | First exported DTC code for slot 1. | raw str; target string | Yes | audit/maintenance only | Mapping r54 | Same schema mismatch as slot 0. | Whether additional DTC codes were truncated. |
| `output` | `event[2].info.dtcCodes[0]` | `event_2_dtc_code_0` | First exported DTC code for slot 2. | raw str; target string | Yes | audit/maintenance only | Mapping r54 | Same schema mismatch as slot 0. | Whether additional DTC codes were truncated. |

## Workbook and schema audit summary

- Sheets: `output` (`A1:BC493745`, 493,744 data rows plus header, 55 columns) and `欄位對照表` (`B1:H82`).
- Data time coverage observed: 2025-01-01 03:56:02 through 2025-11-30 23:53:54; `time` has no timezone marker.
- Main identifiers: `enabledCode` for vehicle and `(enabledCode, journeyCode)` for journey. `carNum` is display/audit identity, not the authoritative trip key.
- GPS fields: longitude, latitude, GPS speed, azimuth, and road speed limit.
- CAN/line fields: CAN status/speed/mileage, fuel level, battery, coolant temperature, cumulative fuel/time, RPM, and engine load.
- Event fields: three flattened slots with type/start/end and type-specific info; the documented event catalog has 20 IDs.
- Nearly all numeric telemetry fields are stored as strings. ETL must parse explicitly and retain failure flags; only `Type`, `can.canStatus`, and event durations are stored numerically in the inspected workbook.

## Data quality findings

### Confirmed/observed issues

- `journeyCode` is not unique across vehicles: 708 codes are shared by more than one `enabledCode`. This is expected per FAQ and proves that journey-only grouping would leak/mix vehicles.
- There are 17 duplicate `(enabledCode, journeyCode, time)` keys. A deterministic duplicate-resolution rule is required before windowing.
- Cadence is irregular rather than a fixed one-row-per-second/minute stream; most within-trip gaps are 1-60 seconds, with a small number above five minutes. Sequence generation needs actual time deltas, masks, completeness rules, and possibly resampling.
- `carNum` is missing on 6,234 rows (1.263%); GPS longitude/latitude are jointly missing on 37 rows.
- `fenceId` is 100% empty, matching FAQ Q1.
- Two `gps.speed` values exceed the documented maximum (max observed 477); 61 `gps.azimuth` values are 360.0 although the documented maximum is 359.9.
- Event-slot nulls are structural and highly asymmetric: slot 0 is used much more than slots 1/2. Missing event-specific info must not be imputed as zero.
- Documented events 5 (fence), 10 (engine overheat), 15 (smoking), and 18 (driver lost) are not observed in this workbook. Absence in this file does not prove impossible production occurrence.
- Event onset/current-state leakage is material: many event `startTime` values equal the telemetry row time, while others precede it. Labels must deduplicate onsets; raw event columns must not be contemporaneous features.

### Checks that passed

- All telemetry `time` values parsed with the documented timestamp shape.
- All observed `journeyCode` values match 12 digits; all `enabledCode` values match the documented 10-character length.
- `carStatus` and `can.canStatus` stay within documented categorical domains.
- Other audited numeric base fields stayed within their documented ranges.
- One nonblank `carNum` mapping per `enabledCode` was observed; the plate is still not the organizer-authoritative vehicle key.

## Source inconsistencies / unresolved schema questions

- `Type` exists in `output` but not in the workbook mapping sheet; FAQ Q2 supplies its definition.
- Mapping declares `journeyCode` as number and multiple fields as misspelled `stirng`; raw storage shows identifiers/numerics largely as strings.
- Mapping describes an event collection, but `output` flattens only three slots. Whether more simultaneous events can be truncated is `UNRESOLVED`.
- Mapping describes fence-event `fenceId`, but no per-event fence ID column exists in `output`; all base fence IDs are empty.
- Mapping calls the DTC field an array `dtcCode`, while output exposes only `dtcCodes[0]`.
- `gps.speedLimit` lacks a documented unit and zero semantics, although the event-specific speed limit is documented in km/h.
- `time` timezone is not documented; only the `journeyCode` encoded hour is explicitly UTC+0.
- Engine load base range (0-250) conflicts with percent wording and the event overload range (0-100).

