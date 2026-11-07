# Shared Locust profiles for the real-load runners; source it, then call load_profile <name>.
# One spawn rate (5 users/s) for every multi-user profile, so ramp-up is under 5 s of a 5-minute run.
load_profile() {
  case "$1" in
    smoke) users=1 spawn=1 default_run_time=1m ;;
    low) users=5 spawn=5 default_run_time=5m ;;
    medium) users=10 spawn=5 default_run_time=5m ;;
    high) users=20 spawn=5 default_run_time=5m ;;
    *) echo "profile must be smoke, low, medium or high" >&2; return 2 ;;
  esac
}
