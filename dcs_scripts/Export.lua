
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
-- Model time of the last world scan that completed (-1: none yet). Sent as "scan_t" with the packet's
-- model time "t": a failed scan leaves the previous lists in every packet, and this shows how old
-- they are (audit step 3, 29 Oct).
local george_scan_t = -1
local GEORGE_CONTACT_RANGE_M = 15000
local GEORGE_CONTACT_MAX = 20
local GEORGE_CARGO_RANGE_M = 500
george_cargo, george_cargo_seen = nil, 0
-- Overlord (common/overlord.py): enemy air-defence radars within 80 km, and the AI flights
-- DCS Olympus spawned for you (group names "Olympus-N")
local GEORGE_AD_RANGE_M = 80000
local GEORGE_AD_MAX = 25
local GEORGE_OLY_MAX = 40
local GEORGE_AD_WORDS = { "shilka", "zsu-23", "tunguska", "2s6", "pantsir", "tor 9a331", "9a331",
    "osa", "9a33", "buk", "9s18", "9s470", "9a310", "kub", "1s91", "2p25", "s-300", "40b6",
    "64h6", "5p85", "snr", "s-125", "p-19", "sa-", "hawk", "patriot", "nasams", "rapier",
    "roland", "gepard", "hq-7", "55g6", "1l13", "dog ear", "ewr", "radar" }
-- ... and for Overlord's CAP / escort / CAS: enemy aircraft within 120 km, enemy ground units within 40 km
local GEORGE_AIR_RANGE_M, GEORGE_AIR_MAX = 120000, 20
local GEORGE_GND_RANGE_M, GEORGE_GND_MAX = 40000, 40
george_ad, george_oly, george_air, george_gnd = "[]", "[]", "[]", "[]"
-- Enemy-side "Olympus-" groups (training targets spawned by the program's Game Master login, audit
-- step 7): [group, unit name, id, lat, lon, alt], so it can find what it spawned and remove only that.
george_olyx = "[]"
local function george_is_ad(name)
    local n = string.lower(name or "")
    for _, w in ipairs(GEORGE_AD_WORDS) do
        if n:find(w, 1, true) then return true end
    end
    return false
end

-- Mission route (LoGetRoute), refreshed every 2 s: '[[n,x,z],...]' and the active waypoint.
local george_route_cache, george_route_next, george_route_t = "[]", -1, -10
local function george_route(now)
    if now - george_route_t < 2.0 then return george_route_cache, george_route_next end
    george_route_t = now
    if not LoGetRoute then
        george_tel_note("noroute", "LoGetRoute not available: no route (home = start point)")
        return george_route_cache, george_route_next
    end
    local ok, r = pcall(LoGetRoute)
    if not ok or type(r) ~= "table" then
        george_tel_note("routefail", "LoGetRoute gave no route: " .. tostring(r))
        return george_route_cache, george_route_next
    end
    local pts = {}
    for i, p in ipairs(r.route or {}) do
        local wp = p.world_point
        if type(wp) == "table" and wp.x and wp.z then
            pts[#pts + 1] = string.format("[%d,%.0f,%.0f]", tonumber(p.this_point_num) or i, wp.x, wp.z)
        end
    end
    george_route_cache = "[" .. table.concat(pts, ",") .. "]"
    george_route_next = (type(r.goto_point) == "table" and tonumber(r.goto_point.this_point_num)) or -1
    george_tel_note("route", "route from LoGetRoute: " .. #pts .. " points, next " .. george_route_next)
    return george_route_cache, george_route_next
end

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
    george_coal = tonumber(my_coal) or 0
    -- Your flight: units of your group that DCS still shows (alive), for wingman orders
    local my_group, my_unit = tostring(me.GroupName or ""), tostring(me.UnitName or "")
    george_my_group, george_my_unit = george_json_str(my_group), george_json_str(my_unit)
    local flight = {}
    local wing = {}                          -- [name, object id, lat, lon] for DCS Olympus orders
    george_self_id = -1
    local mx, mz = me.Position.x, me.Position.z
    local cargo, cargo_d = nil, GEORGE_CARGO_RANGE_M
    george_cargo_seen = 0
    local ad, oly, air, gnd, olyx = {}, {}, {}, {}, {}
    for id, o in pairs(objs) do
        if type(o) == "table" and o.Position and o.LatLongAlt and o.CoalitionID ~= my_coal
           and type(o.Type) == "table" and (o.Type.level1 == 1 or o.Type.level1 == 2) then
            local ex, ez = o.Position.x - me.Position.x, o.Position.z - me.Position.z
            local ed = math.sqrt(ex * ex + ez * ez)
            local ell = o.LatLongAlt
            -- aircraft only (level2 1 planes, 2 helicopters), of a side, with a name: 23 Oct, nameless
            -- level1 = 1 objects at 0 km (flares?) gave "pop-up group, 2 ship target, 0 kilometres"
            if o.Type.level1 == 1 and (o.Type.level2 == 1 or o.Type.level2 == 2) and (o.CoalitionID or 0) > 0
               and tostring(o.Name or "") ~= "" and ed <= GEORGE_AIR_RANGE_M then
                air[#air + 1] = { d = ed, s = string.format('["%s",%d,%.6f,%.6f,%.0f]', george_json_str(o.Name),
                    tonumber(id) or -1, ell.Lat or 0, ell.Long or 0, ell.Alt or 0) }
            elseif o.Type.level1 == 2 and ed <= GEORGE_GND_RANGE_M then
                -- + altitude (m): a ground unit's altitude is the ground's elevation (JTAC target elevation)
                gnd[#gnd + 1] = { d = ed, s = string.format('["%s",%d,%.6f,%.6f,%.0f]', george_json_str(o.Name),
                    tonumber(id) or -1, ell.Lat or 0, ell.Long or 0, ell.Alt or 0) }
            end
        end
        if type(o) == "table" and o.Position and o.LatLongAlt then
            local gname = tostring(o.GroupName or "")
            if o.CoalitionID == my_coal and gname:sub(1, 8) == "Olympus-" and #oly < GEORGE_OLY_MAX then
                local ll = o.LatLongAlt
                oly[#oly + 1] = string.format('["%s","%s",%d,%.6f,%.6f,%.0f]', george_json_str(gname),
                    george_json_str(o.UnitName), tonumber(id) or -1, ll.Lat or 0, ll.Long or 0, ll.Alt or 0)
            elseif o.CoalitionID ~= my_coal and gname:sub(1, 8) == "Olympus-" and #olyx < GEORGE_OLY_MAX then
                local ll = o.LatLongAlt
                olyx[#olyx + 1] = string.format('["%s","%s",%d,%.6f,%.6f,%.0f]', george_json_str(gname),
                    george_json_str(o.UnitName), tonumber(id) or -1, ll.Lat or 0, ll.Long or 0, ll.Alt or 0)
            end
            if o.CoalitionID ~= my_coal and type(o.Type) == "table" and o.Type.level1 == 2
                   and george_is_ad(o.Name) then
                local adx, adz = o.Position.x - me.Position.x, o.Position.z - me.Position.z
                local add = math.sqrt(adx * adx + adz * adz)
                if add <= GEORGE_AD_RANGE_M then
                    ad[#ad + 1] = { d = add, s = string.format('["%s",%d,%.6f,%.6f,%.0f]', george_json_str(o.Name),
                        tonumber(id) or -1, o.LatLongAlt.Lat or 0, o.LatLongAlt.Long or 0, o.LatLongAlt.Alt or 0) }
                end
            end
        end
        -- Sling-load cargo (any coalition): nearest object whose name or type says "cargo"
        local is_cargo = false
        if type(o) == "table" and o.Position then
            local nm = string.lower(tostring(o.Name or "") .. " " .. tostring(o.UnitName or ""))
            is_cargo = nm:find("cargo", 1, true) ~= nil
            if is_cargo then
                george_cargo_seen = george_cargo_seen + 1
                local cdx, cdz = o.Position.x - mx, o.Position.z - mz
                local cd = math.sqrt(cdx * cdx + cdz * cdz)
                if cd < cargo_d then cargo, cargo_d = o, cd end
            end
        end
        if type(o) == "table" and my_group ~= "" and tostring(o.GroupName or "") == my_group then
            if tostring(o.UnitName or "") == my_unit then
                george_self_id = tonumber(id) or -1
            else
                flight[#flight + 1] = '"' .. george_json_str(o.UnitName) .. '"'
                local ll = o.LatLongAlt or {}
                wing[#wing + 1] = string.format('["%s",%d,%.6f,%.6f]', george_json_str(o.UnitName),
                    tonumber(id) or -1, ll.Lat or 0, ll.Long or 0)
            end
        end
        if not is_cargo and type(o) == "table" and o.Position and o.CoalitionID ~= my_coal
           and type(o.Type) == "table" and (o.Type.level1 == 2 or o.Type.level1 == 3) then
            local dx, dz = o.Position.x - mx, o.Position.z - mz
            local dist = math.sqrt(dx * dx + dz * dz)
            if dist <= GEORGE_CONTACT_RANGE_M then
                local brg = math.deg(math.atan2(dz, dx)) % 360     -- DCS: x north, z east
                local ll = o.LatLongAlt or {}
                out[#out + 1] = { id = tonumber(id) or (#out + 1), type = george_json_str(o.Name),
                                  dist_m = math.floor(dist), brg = math.floor(brg),
                                  lat = ll.Lat or 0, lon = ll.Long or 0 }
            end
        end
    end
    george_cargo = cargo and { cargo.Position.x, cargo.Position.y, cargo.Position.z } or nil
    george_flight = "[" .. table.concat(flight, ",") .. "]"
    george_wing = "[" .. table.concat(wing, ",") .. "]"
    table.sort(ad, function(a, b) return a.d < b.d end)
    local ads = {}
    for i = 1, math.min(#ad, GEORGE_AD_MAX) do ads[i] = ad[i].s end
    george_ad = "[" .. table.concat(ads, ",") .. "]"
    george_oly = "[" .. table.concat(oly, ",") .. "]"
    george_olyx = "[" .. table.concat(olyx, ",") .. "]"
    local function nearest(list, max)
        table.sort(list, function(a, b) return a.d < b.d end)
        local o = {}
        for i = 1, math.min(#list, max) do o[i] = list[i].s end
        return "[" .. table.concat(o, ",") .. "]"
    end
    george_air, george_gnd = nearest(air, GEORGE_AIR_MAX), nearest(gnd, GEORGE_GND_MAX)
    george_tel_note("ad" .. math.min(#ad, 1), string.format("air-defence radars within %d km: %d; Olympus flights' units: %d",
        GEORGE_AD_RANGE_M / 1000, #ad, #oly))
    george_tel_note("flight" .. #flight, string.format("your group '%s': %d other unit(s) alive: %s",
        my_group, #flight, george_flight))
    george_tel_note("cargo", "cargo objects in LoGetWorldObjects: " .. george_cargo_seen ..
        " (0 = sling-load talk-on can't see cargo in this mission/DCS version)")
    table.sort(out, function(a, b) return a.dist_m < b.dist_m end)
    while #out > GEORGE_CONTACT_MAX do table.remove(out) end
    george_tel_note("contacts", string.format("contacts from LoGetWorldObjects: %d within %d m%s",
        #out, GEORGE_CONTACT_RANGE_M, #out > 0 and (", e.g. " .. out[1].type .. " at " .. out[1].dist_m .. " m") or ""))
    george_contacts_cache = out
    george_scan_t = now
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

-- Your latitude / longitude (LoGetSelfData().LatLongAlt), for DCS Olympus orders
local function george_ll(ll)
    if type(ll) ~= "table" or type(ll.Lat) ~= "number" or type(ll.Long) ~= "number" then return "null" end
    return string.format("[%.6f,%.6f]", ll.Lat, ll.Long)
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
        items[#items + 1] = string.format('{"id":%d,"type":"%s","dist_m":%d,"brg":%d,"lat":%.6f,"lon":%.6f}',
            t.id, t.type, t.dist_m, t.brg, t.lat, t.lon)
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
    -- Own position (DCS x north, z east, y up; metres) and velocity over the ground (m/s)
    local pos, vel = "null", "null"
    local me_ll = nil
    if LoGetSelfData then
        local ok, me = pcall(LoGetSelfData)
        if ok and type(me) == "table" and type(me.Position) == "table" then
            pos = string.format("[%.1f,%.1f,%.1f]", me.Position.x, me.Position.z, me.Position.y)
            me_ll = me.LatLongAlt
        end
    end
    if LoGetVectorVelocity then
        local ok, v = pcall(LoGetVectorVelocity)
        if ok and type(v) == "table" then vel = string.format("[%.2f,%.2f,%.2f]", v.x or 0, v.z or 0, v.y or 0) end
    end
    -- Fuel from LoGetEngineInfo. Its units differ between modules (kg, or a 0..1 fraction), so
    -- it's sent raw; the first value is noted in the log and the program works out which it is.
    local fuel = "null"
    if LoGetEngineInfo then
        local ok, e = pcall(LoGetEngineInfo)
        if ok and type(e) == "table" and type(e.fuel_internal) == "number" then
            local ext = type(e.fuel_external) == "number" and e.fuel_external or 0
            fuel = string.format("[%.4f,%.4f]", e.fuel_internal, ext)
            george_tel_note("fuel", string.format("LoGetEngineInfo fuel: internal %.4f, external %.4f", e.fuel_internal, ext))
        end
    end
    local route, wp_next = george_route(now)
    local cargo = george_cargo and string.format("[%.1f,%.1f,%.1f]", george_cargo[1], george_cargo[3], george_cargo[2]) or "null"
    local payload = string.format(
        '{"ac":"%s","alt_ft":%.0f,"agl_ft":%.0f,"vs_fpm":%.0f,"speed_kts":%.0f,"pitch":%.1f,"roll":%.1f,"yaw":%.1f,"hdg":%.1f,"ammo":[%d,%d,%d],"contacts":[%s],"pos":%s,"vel":%s,"route":%s,"wp":%d,"cargo":%s,"fuel":%s,"flight":%s,"group":"%s","unit":"%s","wing":%s,"self_id":%d,"ll":%s,"ad":%s,"oly":%s,"air":%s,"gnd":%s,"coal":%d,"t":%.2f,"scan_t":%.2f,"olyx":%s}',
        ac, alt * 3.28084, agl, vs, ias * 1.94384,
        (pitch or 0) * rad, (roll or 0) * rad, (yaw or 0) * rad, hdg, msl, rkt, gun,
        table.concat(items, ","), pos, vel, route, wp_next, cargo, fuel, george_flight or "null", george_my_group or "", george_my_unit or "",
        george_wing or "null", george_self_id or -1, george_ll(me_ll), george_ad or "[]", george_oly or "[]", george_air or "[]", george_gnd or "[]", george_coal or 0,
        now or 0, george_scan_t or -1, george_olyx or "[]")

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

pcall(function() local olympusLFS=require('lfs');dofile(olympusLFS.writedir()..[[Mods\Services\Olympus\Scripts\OlympusCameraControl.lua]]); end,nil) 
