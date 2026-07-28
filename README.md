Fire Evacuation API
This API takes information from the California Governor's Office of Emergency Services and lets the user know if they should evacuate given their location. The database updates every 5 minutes.

Query: 
curl "https://fire-api-424e.onrender.com/api/v1/evacuate?lat=35.7934&lon=-118.6232"

Status meaning:
EVACUATE_NOW - inside an active Evacuation Order polygon
EVACUATE - inside an Evacuation Warning polygon
SHELTER - A zone is within 5 miles
MONITOR - A zone is within radius < 30 mi
SAFE - Nothing 

