#!/bin/zsh
# Call the ErsatzTV API with the local API key (read from the ErsatzTV config folder).
# usage: etv-api.sh METHOD /api/path [json-body]
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/Library/Application Support/ersatztv/api-secrets.json')))['ApiKey'])")
if [ -n "$3" ]; then
  curl -s -X "$1" "http://localhost:8409$2" -H "X-Etv-Api-Key: $KEY" -H 'Content-Type: application/json' -d "$3"
else
  curl -s -X "$1" "http://localhost:8409$2" -H "X-Etv-Api-Key: $KEY"
fi
echo
