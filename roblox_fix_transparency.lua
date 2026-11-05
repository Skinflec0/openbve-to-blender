-- Run in Roblox Studio's Command Bar after importing the GLB.
-- Select the imported model first (or select nothing to process the whole workspace).
--
-- Studio's importer drops the texture alpha, and Roblox only blends alpha when
-- AlphaMode = Transparency AND MeshPart.Transparency > 0. Transparency makes Roblox sort the
-- part against other transparent parts, so overlapping ones flicker or vanish. To keep that
-- to a minimum this only touches parts that really have cut-outs:
--   * parts/materials whose name contains "cutout" (the converter adds that tag), or
--   * if nothing is tagged: every MeshPart that is SELECTED and has a SurfaceAppearance.
local TRANSPARENCY = 0.02   -- raise towards 0.09 only if cut-outs still show solid

local Selection = game:GetService("Selection")
local selected = Selection:Get()
local roots = #selected > 0 and selected or { workspace }

local function isPart(d) return d:IsA("MeshPart") and d:FindFirstChildOfClass("SurfaceAppearance") ~= nil end
local function tagged(d)
	local sa = d:FindFirstChildOfClass("SurfaceAppearance")
	return string.find(string.lower(d.Name), "cutout") ~= nil
		or (sa ~= nil and string.find(string.lower(sa.Name), "cutout") ~= nil)
end

local parts, anyTagged = {}, false
for _, root in ipairs(roots) do
	local list = root:GetDescendants()
	table.insert(list, root)
	for _, d in ipairs(list) do
		if isPart(d) then
			table.insert(parts, d)
			if tagged(d) then anyTagged = true end
		end
	end
end

local count = 0
for _, d in ipairs(parts) do
	local use = anyTagged and tagged(d) or (not anyTagged and #selected > 0)
	if use then
		d:FindFirstChildOfClass("SurfaceAppearance").AlphaMode = Enum.AlphaMode.Transparency
		d.Transparency = TRANSPARENCY
		count += 1
	end
end
if count == 0 then
	warn("No cut-out parts found. Select just the window/strap parts and run again.")
else
	print("Cut-out transparency applied to " .. count .. " MeshPart(s)" ..
		(anyTagged and " (tagged parts only)" or " (selected parts)"))
end
