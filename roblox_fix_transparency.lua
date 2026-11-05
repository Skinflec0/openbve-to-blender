-- Run in Roblox Studio's Command Bar after importing the GLB.
-- Select the imported model first (or select nothing to process the whole workspace).
-- Studio's importer ignores the texture alpha channel; this switches it on so windows,
-- doors and other cut-outs become see-through instead of solid.
local Selection = game:GetService("Selection")
local roots = Selection:Get()
if #roots == 0 then roots = { workspace } end
local count = 0
for _, root in ipairs(roots) do
	local list = root:GetDescendants()
	table.insert(list, root)
	for _, d in ipairs(list) do
		if d:IsA("SurfaceAppearance") then
			d.AlphaMode = Enum.AlphaMode.Transparency
			count += 1
		end
	end
end
print("AlphaMode set to Transparency on " .. count .. " SurfaceAppearance object(s)")
