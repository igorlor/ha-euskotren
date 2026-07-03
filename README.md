# Euskotren Next Trains

Home Assistant custom integration to show next Euskotren trains for a given stop and direction using GTFS-RT TripUpdates and local GTFS static files.

## Installation using HACS

1. Open HACS.
2. Go to Integrations.
3. Open the three-dot menu.
4. Select Custom repositories.
5. Add this repository URL:

   `https://github.com/TU_USUARIO/ha-euskotren-next-trains`

6. Select category: Integration.
7. Install the integration.
8. Restart Home Assistant.

## Manual configuration

Add to `configuration.yaml`:

```yaml
sensor:
  - platform: euskotren_next_trains
    name: Euskotren Zurbaranbarri Kukullaga
    gtfs_dir: /config/gtfs/euskotren
    stop_name: "Zurbaranbarri-Bilbao"
    direction: "Kukullaga"
    scan_interval: 30
    max_trains: 5
