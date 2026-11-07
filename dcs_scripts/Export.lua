
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
-- Instructor Mode (I5): while this file exists the program wants flight data faster, so a small
-- flight-only packet goes out at 10 Hz between the full ones. Checked once a second; the program
-- creates it for an exercise and deletes it after. Without it nothing below runs.
local george_fast_flag = lfs.writedir() .. [[Scripts\george_fast_telemetry.flag]]
local george_fast_on, george_fast_check, george_fast_last = false, -1, 0
local george_gmax = nil                 -- peak normal g since the last fast packet (read every frame)

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
-- Tactical picture I-T2 / I-T3 (1 Nov): neutrals and your own side, so neither is taken for an enemy.
-- neu: units of neither side (ground / naval within 40 km), fr: your side's units (not your group, not
-- Olympus- flights) - ground / naval within 40 km, aircraft within 120 km. [type, id, lat, lon, alt, "a"|"g"|"n"]
local GEORGE_SIDE_MAX = 40
george_neu, george_fr = "[]", "[]"
-- Enemy = the other side (1 red, 2 blue). Units of neither side are neutral, not enemies. If your own
-- side isn't 1 or 2 (spectator?), everything not yours, as before.
local function george_enemy(c, mine)
    if mine == 1 or mine == 2 then return (c == 1 or c == 2) and c ~= mine end
    return c ~= mine
end
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
    local neu, fr = {}, {}
    for id, o in pairs(objs) do
        if type(o) == "table" and o.Position and o.LatLongAlt and george_enemy(o.CoalitionID, my_coal)
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
            if george_enemy(o.CoalitionID, my_coal) and type(o.Type) == "table" and o.Type.level1 == 2
                   and george_is_ad(o.Name) then
                local adx, adz = o.Position.x - me.Position.x, o.Position.z - me.Position.z
                local add = math.sqrt(adx * adx + adz * adz)
                if add <= GEORGE_AD_RANGE_M then
                    ad[#ad + 1] = { d = add, s = string.format('["%s",%d,%.6f,%.6f,%.0f]', george_json_str(o.Name),
                        tonumber(id) or -1, o.LatLongAlt.Lat or 0, o.LatLongAlt.Long or 0, o.LatLongAlt.Alt or 0) }
                end
            end
        end
        -- Neutrals and your own side (I-T2 / I-T3): aircraft (level2 1 / 2), ground, ships, with a name
        if type(o) == "table" and o.Position and o.LatLongAlt and type(o.Type) == "table"
           and tostring(o.Name or "") ~= "" and (o.Type.level1 == 2 or o.Type.level1 == 3
               or (o.Type.level1 == 1 and (o.Type.level2 == 1 or o.Type.level2 == 2))) then
            local sx, sz = o.Position.x - mx, o.Position.z - mz
            local sd = math.sqrt(sx * sx + sz * sz)
            local cat = (o.Type.level1 == 1 and "a") or (o.Type.level1 == 3 and "n") or "g"
            local sgrp = tostring(o.GroupName or "")
            local sll = o.LatLongAlt
            local row = { d = sd, s = string.format('["%s",%d,%.6f,%.6f,%.0f,"%s"]', george_json_str(o.Name),
                tonumber(id) or -1, sll.Lat or 0, sll.Long or 0, sll.Alt or 0, cat) }
            if o.CoalitionID == my_coal then
                if (my_group == "" or sgrp ~= my_group) and sgrp:sub(1, 8) ~= "Olympus-"
                   and tostring(o.UnitName or "") ~= my_unit
                   and sd <= (cat == "a" and GEORGE_AIR_RANGE_M or GEORGE_GND_RANGE_M) then
                    fr[#fr + 1] = row
                end
            elseif not george_enemy(o.CoalitionID, my_coal) and cat ~= "a" and sd <= GEORGE_GND_RANGE_M then
                neu[#neu + 1] = row
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
        if not is_cargo and type(o) == "table" and o.Position and george_enemy(o.CoalitionID, my_coal)
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
    george_neu, george_fr = nearest(neu, GEORGE_SIDE_MAX), nearest(fr, GEORGE_SIDE_MAX)
    george_tel_note("side" .. math.min(#neu, 1) .. math.min(#fr, 1), string.format(
        "neutral units within %d km: %d; your side's units in range: %d", GEORGE_GND_RANGE_M / 1000, #neu, #fr))
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

-- Normal acceleration in g (body y axis), or nil when the module doesn't give it (I1)
local function george_g()
    if not LoGetAccelerationUnits then return nil end
    local ok, a = pcall(LoGetAccelerationUnits)
    if ok and type(a) == "table" and type(a.y) == "number" then return a.y end
    return nil
end

local function george_num(v, fmt)
    if type(v) ~= "number" or v ~= v then return "null" end
    return string.format(fmt, v)
end

-- The 10 Hz flight-only packet: "k":"f" tells the program it carries no contacts, route or lists
local function george_send_fast(now)
    if not LoGetSelfData then return end
    local ok, me = pcall(LoGetSelfData)
    if not ok or type(me) ~= "table" or type(me.Position) ~= "table" then return end
    local rad = 180 / math.pi
    local agl, vs, ias = nil, nil, nil
    if LoGetAltitudeAboveGroundLevel then
        local ok2, v = pcall(LoGetAltitudeAboveGroundLevel); if ok2 then agl = v end
    end
    if LoGetVerticalVelocity then
        local ok2, v = pcall(LoGetVerticalVelocity); if ok2 then vs = v end
    end
    if LoGetIndicatedAirSpeed then
        local ok2, v = pcall(LoGetIndicatedAirSpeed); if ok2 then ias = v end
    end
    local vel = "null"
    if LoGetVectorVelocity then
        local ok2, v = pcall(LoGetVectorVelocity)
        if ok2 and type(v) == "table" then vel = string.format("[%.2f,%.2f,%.2f]", v.x or 0, v.z or 0, v.y or 0) end
    end
    local g = george_g()
    local gmax = george_gmax or g
    george_gmax = nil
    local cargo = george_cargo and string.format("[%.1f,%.1f,%.1f]", george_cargo[1], george_cargo[3], george_cargo[2]) or "null"
    local payload = string.format(
        '{"k":"f","t":%.3f,"pos":[%.2f,%.2f,%.2f],"vel":%s,"agl_ft":%s,"vs_fpm":%s,"speed_kts":%s,"hdg":%s,"pitch":%s,"roll":%s,"g":%s,"gmax":%s,"ll":%s,"cargo":%s}',
        now, me.Position.x, me.Position.z, me.Position.y, vel,
        george_num(agl and agl * 3.28084, "%.1f"), george_num(vs and vs * 196.85, "%.0f"),
        george_num(ias and ias * 1.94384, "%.1f"),
        george_num(type(me.Heading) == "number" and (me.Heading * rad) % 360 or nil, "%.1f"),
        george_num(type(me.Pitch) == "number" and me.Pitch * rad or nil, "%.1f"),
        george_num(type(me.Bank) == "number" and me.Bank * rad or nil, "%.1f"),
        george_num(g, "%.2f"), george_num(gmax, "%.2f"), george_ll(me.LatLongAlt), cargo)
    local sent = pcall(udp.send, udp, payload)
    if sent then george_tel_note("fastsent", "first 10 Hz flight packet sent (instructor exercise)") end
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
    -- Magnetic variation here (true - magnetic, degrees, east positive), for magnetic headings in the
    -- JTAC's 9-line (5 Nov). nil when DCS doesn't give the magnetic heading.
    local magvar = nil
    if LoGetMagneticYaw and hdg >= 0 then
        local ok, my = pcall(LoGetMagneticYaw)
        if ok and type(my) == "number" then magvar = ((hdg - my * rad + 540) % 360) - 180 end
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
        '{"ac":"%s","alt_ft":%.0f,"agl_ft":%.0f,"vs_fpm":%.0f,"speed_kts":%.0f,"pitch":%.1f,"roll":%.1f,"yaw":%.1f,"hdg":%.1f,"ammo":[%d,%d,%d],"contacts":[%s],"pos":%s,"vel":%s,"route":%s,"wp":%d,"cargo":%s,"fuel":%s,"flight":%s,"group":"%s","unit":"%s","wing":%s,"self_id":%d,"ll":%s,"ad":%s,"oly":%s,"air":%s,"gnd":%s,"coal":%d,"t":%.2f,"scan_t":%.2f,"olyx":%s,"g":%s,"neu":%s,"fr":%s,"magvar":%s}',
        ac, alt * 3.28084, agl, vs, ias * 1.94384,
        (pitch or 0) * rad, (roll or 0) * rad, (yaw or 0) * rad, hdg, msl, rkt, gun,
        table.concat(items, ","), pos, vel, route, wp_next, cargo, fuel, george_flight or "null", george_my_group or "", george_my_unit or "",
        george_wing or "null", george_self_id or -1, george_ll(me_ll), george_ad or "[]", george_oly or "[]", george_air or "[]", george_gnd or "[]", george_coal or 0,
        now or 0, george_scan_t or -1, george_olyx or "[]", george_num(george_g(), "%.2f"),
        george_neu or "[]", george_fr or "[]", george_num(magvar, "%.1f"))

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

-- Detection probe D0 (3 Nov): your own aircraft's sensors as Export.lua sees them (RWR, radar targets,
-- locked targets, sighting system), every 5 s, ONLY while Scripts\george_detection_probe.flag exists.
-- Unknown tables are dumped (scalars, one level deep) so the first flight shows what each module fills.
-- Writes Logs\george_detection_probe_export.txt; sends nothing.
local george_probe_flag = lfs.writedir() .. [[Scripts\george_detection_probe.flag]]
local george_probe_path = lfs.writedir() .. [[Logs\george_detection_probe_export.txt]]
local george_probe_next, george_probe_on = -1, false

local function george_dump(t, depth)
    if type(t) ~= "table" then return tostring(t) end
    local parts, n = {}, 0
    for k, v in pairs(t) do
        n = n + 1
        if n > 20 then parts[#parts + 1] = "..." break end
        if type(v) == "table" then
            parts[#parts + 1] = tostring(k) .. "=" .. ((depth or 0) < 1 and george_dump(v, (depth or 0) + 1) or "{...}")
        else
            parts[#parts + 1] = tostring(k) .. "=" .. tostring(v)
        end
    end
    return "{" .. table.concat(parts, ", ") .. "}"
end

local function george_probe_sensors(now)
    local f = io.open(george_probe_path, george_probe_on and "a" or "w")
    george_probe_on = true
    if not f then return end
    local function line(s) f:write(string.format("%.1f  %s\n", now, s)) end
    local function try(name, fn)
        if not fn then line(name .. ": not available") return nil end
        local ok, v = pcall(fn)
        if not ok then line(name .. ": error " .. tostring(v)) return nil end
        if v == nil then line(name .. ": nil") end
        return v
    end
    local ok_s, me = pcall(LoGetSelfData)
    line("aircraft " .. tostring(ok_s and type(me) == "table" and me.Name or "?"))
    local tws = try("LoGetTWSInfo", LoGetTWSInfo)
    if type(tws) == "table" then
        local em = type(tws.Emitters) == "table" and tws.Emitters or {}
        line(string.format("RWR mode %s, %d emitters", tostring(tws.Mode), #em))
        for i, e in ipairs(em) do
            if i > 10 then break end
            line("  RWR " .. george_dump(e))
        end
    end
    local tg = try("LoGetTargetInformation", LoGetTargetInformation)
    if type(tg) == "table" then
        line(string.format("radar targets %d", #tg))
        for i, t in ipairs(tg) do
            if i > 10 then break end
            line("  TGT " .. george_dump(t))
        end
    end
    local lk = try("LoGetLockedTargetInformation", LoGetLockedTargetInformation)
    if type(lk) == "table" then
        line(string.format("locked targets %d", #lk))
        for i, t in ipairs(lk) do
            if i > 5 then break end
            line("  LOCK " .. george_dump(t))
        end
    end
    local si = try("LoGetSightingSystemInfo", LoGetSightingSystemInfo)
    if type(si) == "table" then line("sight " .. george_dump(si)) end
    f:close()
end

function LuaExportBeforeNextFrame()
    if not udp then return end
    local now = (LoGetModelTime and LoGetModelTime()) or os.clock()
    if now < george_tel_last then george_tel_last, george_fast_last, george_fast_check = 0, 0, -1 end   -- new mission
    if now - george_fast_check >= 1.0 or now < george_fast_check then
        george_fast_check = now
        george_fast_on = lfs.attributes(george_fast_flag) ~= nil
    end
    if now >= george_probe_next or now + 30 < george_probe_next then       -- also after a new mission
        george_probe_next = now + 5.0
        if lfs.attributes(george_probe_flag) ~= nil then
            local ok, err = pcall(george_probe_sensors, now)
            if not ok then george_tel_note("probeerr", "detection probe error: " .. tostring(err)) end
        else
            george_probe_on = false
        end
    end
    if george_fast_on then
        local g = george_g()
        if g and (george_gmax == nil or g > george_gmax) then george_gmax = g end
    end
    if now - george_tel_last >= 0.25 then
        george_tel_last, george_fast_last = now, now
        local ok, err = pcall(george_send_telemetry)
        if not ok then george_tel_note("error", "telemetry error: " .. tostring(err)) end
    elseif george_fast_on and now - george_fast_last >= 0.1 then
        george_fast_last = now
        local ok, err = pcall(george_send_fast, now)
        if not ok then george_tel_note("fasterror", "fast telemetry error: " .. tostring(err)) end
    end
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
