
local SimShakerlfs=require('lfs'); dofile(SimShakerlfs.writedir()..'Scripts/SimShaker.lua')

dofile(lfs.writedir()..[[Scripts\SimShaker-export-core\ExportCore.lua]])

pcall(function() local dcsSr=require('lfs');dofile(dcsSr.writedir()..[[Mods\Services\DCS-SRS\Scripts\DCS-SimpleRadioStandalone.lua]]); end,nil)


-- DCS Telemetry Exporter for Local AI Middleware (George)
-- FIX: the previous version called LoGetTargetList(), which does not exist in the DCS Export API.
-- It raised an error on EVERY frame, before the UDP send, so nothing was ever sent on port 5005.
-- This version never raises: flight data is always sent, targets are best-effort, and what
-- happened is written to Logs\george_telemetry_status.txt. Sends 4x per second, not every frame.
local socket = require("socket")
local udp = nil
local george_tel_last = 0
local george_tel_noted = {}
local george_tel_path = lfs.writedir() .. [[Logs\george_telemetry_status.txt]]

local function george_tel_note(key, msg)
    if george_tel_noted[key] then return end
    george_tel_noted[key] = true
    local f = io.open(george_tel_path, "a")
    if f then
        f:write(os.date("%H:%M:%S") .. "  " .. msg .. "\n")
        f:close()
    end
end

local function george_json_str(s)
    return (tostring(s or "UNKNOWN"):gsub('[%c"\\]', ""))
end

-- Nearby enemy units {id, type, dist_m, brg}, nearest first, refreshed once per second.
-- The AH-64D does not fill LoGetLockedTargetInformation / LoGetTargetInformation from George's
-- TADS searches, so George's own list comes from george_ai_export.lua. These "contacts" only
-- give the middleware ranges for the names George reports. Never raises.
local george_contacts_cache = {}
local george_contacts_next = 0
local GEORGE_CONTACT_RANGE_M = 15000
local GEORGE_CONTACT_MAX = 20

local function george_contacts(now)
    if now < george_contacts_next then return george_contacts_cache end
    george_contacts_next = now + 1.0
    local out = {}
    if not (LoGetWorldObjects and LoGetSelfData) then
        george_tel_note("noworld", "LoGetWorldObjects or LoGetSelfData not available: no contacts")
        george_contacts_cache = out
        return out
    end
    local ok_s, me = pcall(LoGetSelfData)
    if not ok_s or type(me) ~= "table" or not me.Position then
        george_contacts_cache = out
        return out
    end
    local ok_w, objs = pcall(LoGetWorldObjects)
    if not ok_w or type(objs) ~= "table" then
        george_tel_note("worldfail", "LoGetWorldObjects failed or blocked: " .. tostring(objs))
        george_contacts_cache = out
        return out
    end
    local my_coal = me.CoalitionID
    local mx, mz = me.Position.x, me.Position.z
    for id, o in pairs(objs) do
        if type(o) == "table" and o.Position and o.CoalitionID ~= my_coal
           and type(o.Type) == "table" and (o.Type.level1 == 2 or o.Type.level1 == 3) then
            local dx, dz = o.Position.x - mx, o.Position.z - mz
            local dist = math.sqrt(dx * dx + dz * dz)
            if dist <= GEORGE_CONTACT_RANGE_M then
                local brg = math.deg(math.atan2(dz, dx)) % 360     -- DCS: x north, z east
                out[#out + 1] = { id = tonumber(id) or (#out + 1), type = george_json_str(o.Name),
                                  dist_m = math.floor(dist), brg = math.floor(brg) }
            end
        end
    end
    table.sort(out, function(a, b) return a.dist_m < b.dist_m end)
    while #out > GEORGE_CONTACT_MAX do table.remove(out) end
    george_tel_note("contacts", string.format("contacts from LoGetWorldObjects: %d within %d m%s",
        #out, GEORGE_CONTACT_RANGE_M, #out > 0 and (", e.g. " .. out[1].type .. " at " .. out[1].dist_m .. " m") or ""))
    george_contacts_cache = out
    return out
end

-- Remaining stores: Hellfires (wsType level2 4), rockets (level2 7), 30 mm rounds. -1 = unknown.
local function george_ammo()
    if not LoGetPayloadInfo then return -1, -1, -1 end
    local ok, p = pcall(LoGetPayloadInfo)
    if not ok or type(p) ~= "table" then return -1, -1, -1 end
    local msl, rkt = 0, 0
    if type(p.Stations) == "table" then
        for _, st in pairs(p.Stations) do
            if type(st) == "table" and type(st.weapon) == "table" and type(st.count) == "number" then
                if st.weapon.level2 == 4 then
                    msl = msl + st.count
                elseif st.weapon.level2 == 7 then
                    rkt = rkt + st.count
                end
            end
        end
    end
    local gun = -1
    if type(p.Cannon) == "table" and type(p.Cannon.shells) == "number" then gun = p.Cannon.shells end
    george_tel_note("ammo", string.format("stores from LoGetPayloadInfo: missiles %d, rockets %d, gun %d", msl, rkt, gun))
    return msl, rkt, gun
end

local function george_send_telemetry()
    local alt = LoGetAltitudeAboveSeaLevel() or 0
    local ias = LoGetIndicatedAirSpeed() or 0
    local pitch, roll, yaw = LoGetADIPitchBankYaw()

    george_tel_note("apis", string.format("APIs present: LoGetWorldObjects=%s LoGetSelfData=%s",
        tostring(LoGetWorldObjects ~= nil), tostring(LoGetSelfData ~= nil)))

    local items = {}
    local now = (LoGetModelTime and LoGetModelTime()) or os.clock()
    for _, t in ipairs(george_contacts(now)) do
        items[#items + 1] = string.format('{"id":%d,"type":"%s","dist_m":%d,"brg":%d}', t.id, t.type, t.dist_m, t.brg)
    end

    local msl, rkt, gun = george_ammo()
    local rad = 180 / math.pi
    -- True heading in degrees (0 = north, clockwise), for clock positions of targets.
    local hdg = -1
    local ac = ""
    if LoGetSelfData then
        local ok, me = pcall(LoGetSelfData)
        if ok and type(me) == "table" then
            if type(me.Heading) == "number" then hdg = (me.Heading * rad) % 360 end
            ac = george_json_str(me.Name or "")
        end
    end
    -- Height above ground (radar-altimeter style) and vertical speed, for crew callouts.
    local agl = -1
    if LoGetAltitudeAboveGroundLevel then
        local ok, v = pcall(LoGetAltitudeAboveGroundLevel)
        if ok and type(v) == "number" then agl = v * 3.28084 end
    end
    local vs = 0
    if LoGetVerticalVelocity then
        local ok, v = pcall(LoGetVerticalVelocity)
        if ok and type(v) == "number" then vs = v * 196.85 end
    end
    local payload = string.format(
        '{"ac":"%s","alt_ft":%.0f,"agl_ft":%.0f,"vs_fpm":%.0f,"speed_kts":%.0f,"pitch":%.1f,"roll":%.1f,"yaw":%.1f,"hdg":%.1f,"ammo":[%d,%d,%d],"contacts":[%s]}',
        ac, alt * 3.28084, agl, vs, ias * 1.94384,
        (pitch or 0) * rad, (roll or 0) * rad, (yaw or 0) * rad, hdg, msl, rkt, gun,
        table.concat(items, ","))

    local ok, err = pcall(udp.send, udp, payload)
    if ok then
        george_tel_note("sent", "first telemetry packet sent")
    else
        george_tel_note("senderr", "send failed: " .. tostring(err))
    end
end

function LuaExportStart()
    udp = socket.udp()
    udp:settimeout(0)
    udp:setpeername("127.0.0.1", 5005)
    george_tel_noted = {}
    local f = io.open(george_tel_path, "w")      -- fresh log each mission
    if f then
        f:write(os.date("%H:%M:%S") .. "  telemetry exporter started\n")
        f:close()
    end
end

function LuaExportBeforeNextFrame()
    if not udp then return end
    local now = (LoGetModelTime and LoGetModelTime()) or os.clock()
    if now < george_tel_last then george_tel_last = 0 end       -- new mission: sim time restarted
    if now - george_tel_last < 0.25 then return end
    george_tel_last = now
    local ok, err = pcall(george_send_telemetry)
    if not ok then george_tel_note("error", "telemetry error: " .. tostring(err)) end
end

function LuaExportStop()
    if udp then
        udp:close()
        udp = nil
    end
end


dofile(lfs.writedir() .. [[Scripts\DCS-BIOS\BIOS.lua]])
dofile(lfs.writedir()..[[Scripts\george_export.lua]])
dofile(lfs.writedir()..[[Scripts\george_ai_export.lua]])
pcall(dofile, lfs.writedir()..[[Scripts\george_huey_export.lua]])   -- UH-1H crew link (optional)
