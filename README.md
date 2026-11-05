# openbve-to-blender

Convert **OpenBVE / BVE Trainsim** train objects into **GLB** files you can open in Blender (and import into Roblox Studio). One Python file, no setup.

- Handles `.b3d`, `.csv`, `.animated` (assembled into complete cars) and text DirectX `.x`
- Embeds textures, including BVE's *transparent colour* cut-outs (windows, doors, grilles)
- Only dependency is [Pillow](https://pypi.org/project/pillow/), which is installed automatically when needed

## Quick start

1. Install [Python 3.8+](https://www.python.org/downloads/).
2. Download `openbve-to-blender.py`.
3. **Double-click it** and pick the train folder (or cancel the folder dialog to pick individual files).
4. Open the output folder `<train folder>_glb/assembled_models/` and import the `.glb` files in Blender: `File > Import > glTF 2.0`.

Command line:

```
python openbve-to-blender.py "C:\Trains\MyTrain"
python openbve-to-blender.py car.b3d part.csv -o out
python openbve-to-blender.py "C:\Trains\MyTrain" --match "9525*"
```

## Options

| Option | What it does |
|---|---|
| `-o, --outdir DIR` | Output folder (default: `<folder>_glb` next to the input) |
| `--combine` | Also write one GLB containing everything |
| `--flip` | Reverse face winding if a file looks inside-out |
| `--parts` | Also write every loose `.b3d`/`.csv`/`.x` part, not only assembled cars |
| `--no-loose` | Skip parts that no `.animated` file uses |
| `--match PATTERN` | Only convert `.animated` files matching a pattern, e.g. `"9525*"` |
| `--split-cutouts` | Write partly transparent parts to a separate `<name>_cutouts.glb` |
| `--keep-cutouts` | Keep triangles that are 100% transparent (default: they are removed) |
| `--double-sided` | Make every face visible from both sides (BVE default: only `Face2`/`AddFace2`) |
| `--key-tolerance N` | How far (0-255) a pixel may differ from the transparent colour and still be cut out. Try `0` if dark parts vanish, `60`-`100` if window cut-outs stay solid |

## Output layout

```
MyTrain_glb/
  assembled_models/           <- start here: one GLB per .animated car
  loose_parts/                <- optional: individual parts, mirrors the source tree
```

Without any `.animated` file, each object file becomes its own GLB.

## Coordinates

BVE is left-handed (+Z forward). The GLB is right-handed with Z mirrored, so in Blender the train points along **+Y** with **+X** to its right. Face winding is converted so normals point outwards.

## Importing into Roblox Studio

**Status: not fully solved.** The GLB imports into Roblox Studio and the textures work, but transparent parts (windows, doors, handstraps, grilles) still look wrong.

What is known:

- Roblox's importer does not reliably keep texture alpha, so BVE's transparent colour (usually blue) can show up as solid blue or grey.
- The converter reduces this: cut-out pixels are filled with the surrounding colour (never the key colour), and triangles that are 100% cut out (typical window panes) are removed so they become real holes. Use `--keep-cutouts` to turn that off.
- `--split-cutouts` writes the partly transparent parts to their own `_cutouts.glb`, which makes them easier to handle separately in Studio.
- Roblox only blends alpha when `SurfaceAppearance.AlphaMode` is `Transparency` **and** `MeshPart.Transparency` is above 0. You can try values from `0.02` to `0.09` on the affected MeshParts, but it is not a clean fix: overlapping transparent parts sort badly and can flicker or disappear.

Blender is not affected by any of this.

## Supported

| Feature | Notes |
|---|---|
| `CreateMeshBuilder`, `[MeshBuilder]` | |
| `Vertex`/`AddVertex`, `Face`/`Face2`/`AddFace`/`AddFace2` | Quads and polygons are triangulated |
| `Cube`, `Cylinder` | |
| `Translate`, `Scale`, `Rotate`, `Shear`, `Mirror` | Plus the `*All` variants. `Rotate` with a zero axis turns about X, like openBVE |
| `Color`, `EmissiveColor`, `BlendMode` | |
| `Load`/`LoadTexture`, `Transparent`/`SetDecalTransparentColor` | Day texture is used |
| `Coordinates`/`SetTextureCoordinates` | |
| `.animated` | Every `[Object]`/`[Include]` placed at its first state and `Position` |
| `.x` (text) | `Mesh`, `MeshMaterialList`, `Material`, `TextureFilename`, `MeshTextureCoords`, `Frame` transforms |
| Texture lookup | Next to the object, then parent folders; falls back to another image extension (`roof.bmp` -> `roof.png`) |
| Encodings | UTF-8, UTF-16/32, Windows-1252, Shift-JIS |

## Not supported

- Animation functions (parts stay at their first state, so doors and pantographs are not animated)
- Binary or compressed `.x` files (re-save them as text `.x`)
- Night textures, `.xml`/`.extensions.cfg` train data, sound

## Troubleshooting

| Problem | Fix |
|---|---|
| Faces look inside-out | Re-run with `--flip` |
| Windows are solid in Blender | Raise `--key-tolerance` (JPEG textures), e.g. `80` |
| Dark parts disappeared | Run with `--key-tolerance 0` |
| Textures are grey or missing | Check the console for `texture not found` and keep the texture files inside the train folder |
| Windows are blue, solid or glitchy in Roblox | Known issue, see [Importing into Roblox Studio](#importing-into-roblox-studio) |
| `no [Object]/[Include] entries found` | The `.animated` file uses an unusual layout; use `--parts` to export the parts instead |

## License

MIT. Use it, change it, share it.
