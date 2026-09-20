# Keep the application database local to the Mac

Status: accepted (2026-09-20)

The personal recommender currently runs on the Mac, with PostgreSQL as its local system of record. Windows needs a build of the app, but is not a second active device. We will keep using the Mac-local database and will not add Mac–Windows database synchronization or move the authoritative database to a cloud service. For one active user and device, cross-device sync would add hosting or service limits, network dependence (including uncertain access while in China), and conflict/recovery work without enough benefit.

Buildability on Windows is separate from data portability: a Windows build does not include the Mac database. If Windows later needs to run with real data, restore a PostgreSQL backup into a Windows-local database. Maintain regular backups and verify that a backup can be restored; this is a recovery and migration path, not live sync. Google Sheets remains the existing one-way projection of managed viewing-history records and is not a backup of the complete application database.

Revisit this decision if both devices become regular places to record feedback or viewing history, and manual database transfer becomes a recurring burden. At that point, define the required offline behavior and conflict rules before choosing a synchronization service or a shared database.
