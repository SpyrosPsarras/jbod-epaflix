# Deal Finder is a long-running app, not a CronJob

epaflix runs its scheduled work as Kubernetes CronJobs (28 of them), so a six-hourly Hunt would normally be one more CronJob. We run the Deal Finder as a long-running Deployment with its own scheduler instead, because the page needs live actions that a CronJob cannot serve: a free-text Search against every Source, "Hunt now", "Track this", and "Mark as bought".
