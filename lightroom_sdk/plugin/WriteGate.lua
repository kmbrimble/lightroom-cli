-- WriteGate.lua
-- catalog:withWriteAccessDo(name, fn, {timeout=N}) does NOT run fn when it
-- returns "aborted" (gate not obtained in time); handlers that call their
-- callback from inside fn then never reply. These helpers turn any status other
-- than "executed" into a WRITE_ABORTED error.

local WriteGate = {}

WriteGate.CODE = "WRITE_ABORTED"

-- nil when status == "executed", else an error reply table.
function WriteGate.abortResponse(status, name)
    if status == "executed" then return nil end
    return {
        success = false,
        error = {
            code = WriteGate.CODE,
            message = WriteGate.CODE .. ": catalog write '" .. tostring(name)
                .. "' did not run (gate result: " .. tostring(status)
                .. "). Lightroom was busy; retry.",
            severity = "error",
        },
    }
end

-- Drop-in for catalog:withWriteAccessDo(name, fn, opts): raises a
-- "WRITE_ABORTED: ..." error unless fn executed. Returns the status.
function WriteGate.run(catalog, name, fn, opts)
    local status = catalog:withWriteAccessDo(name, fn, opts)
    local r = WriteGate.abortResponse(status, name)
    if r then error(r.error.message, 0) end
    return status
end

-- Error code for a failed write: WRITE_ABORTED / GUARD_DENIED are recognised in
-- the error text, otherwise `default`.
function WriteGate.codeFor(err, default)
    local s = tostring(err)
    if string.find(s, "WRITE_ABORTED", 1, true) then return "WRITE_ABORTED" end
    if string.find(s, "GUARD_DENIED", 1, true) then return "GUARD_DENIED" end
    return default
end

return WriteGate
