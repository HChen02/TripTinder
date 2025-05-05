#!/bin/bash
ORIGIN=$1
DESTINATION=$2
CORE_INDEX=$3

API_KEY="sh967490139224896692439644109194"

echo "[Core $CORE_INDEX] Origin: $ORIGIN, Destination: $DESTINATION"

# Step 1: Create search session
CREATE_RESPONSE=$(curl --silent --request POST "https://partners.api.skyscanner.net/apiservices/v3/flights/live/search/create" \
  --header "x-api-key: $API_KEY" \
  --header "Content-Type: application/json" \
  --data @<(cat <<EOF
{
  "query": {
    "market": "UK",
    "locale": "en-GB",
    "currency": "GBP",
    "queryLegs": [
      {
        "originPlaceId": { "iata": "$ORIGIN" },
        "destinationPlaceId": { "iata": "$DESTINATION" },
        "date": { "year": 2025, "month": 12, "day": 22 }
      }
    ],
    "adults": 1,
    "cabinClass": "CABIN_CLASS_ECONOMY"
  }
}
EOF
))

# CREATE_RESPONSE=$(curl -s -X POST "https://partners.api.skyscanner.net/apiservices/v3/flights/live/search/create" \
#   -H "x-api-key: $API_KEY" \
#   -H "Content-Type: application/json" \
#   -d '{
#     "query": {
#       "market": "UK",
#       "locale": "en-GB",
#       "currency": "GBP",
#       "queryLegs": [
#         {
#           "originPlaceId": { "iata": "$ORIGIN" },
#           "destinationPlaceId": { "iata": "SIN" },
#           "date": { "year": 2025, "month": 12, "day": 22 }
#         }
#       ],
#       "adults": 1,
#       "cabinClass": "CABIN_CLASS_ECONOMY"
#     }
#   }')

SESSION_TOKEN=$(echo "$CREATE_RESPONSE" | jq -r '.sessionToken')

if [ "$SESSION_TOKEN" == "null" ] || [ -z "$SESSION_TOKEN" ]; then
  echo "Failed to get session token"
  echo "$CREATE_RESPONSE"
  exit 1
fi

echo "Session token: $SESSION_TOKEN"

# Step 2: Poll for results
POLL_URL="https://partners.api.skyscanner.net/apiservices/v3/flights/live/search/poll/$SESSION_TOKEN"
echo "Polling session..."

for i in {1..100}; do
  POLL_RESPONSE=$(curl -s -X POST "$POLL_URL" -H "x-api-key: $API_KEY")
  STATUS=$(echo "$POLL_RESPONSE" | jq -r '.status')

  echo "Status: $STATUS"

  if [ "$STATUS" == "RESULT_STATUS_COMPLETE" ]; then
    echo "$POLL_RESPONSE" | jq '.' > results_$CORE_INDEX.txt
    echo "Results saved to results_$CORE_INDEX.txt"
    exit 0
  fi
  sleep 3
done

echo "Polling timed out."

