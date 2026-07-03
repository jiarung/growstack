FROM nodered/node-red:4.0

# node-red-dashboard supplies the ui_dropdown / ui_button / ui_text nodes used by the
# starter flow's /ui page. Everything else in the flow is core nodes.
RUN npm install node-red-dashboard@3

# Seed the starter flow. /data is a named volume: docker copies image content into
# an EMPTY volume on first creation only, so a stale volume must be removed
# (docker volume rm broker_nodered-data) to pick up flow changes from git.
COPY --chown=node-red:node-red flows.json /data/flows.json
