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
| `--double-sided` | Make every face visible from both sides (BVE default: only `Face2`/`AddFace2`) |
| `--key-tolerance N` | How far (0-255) a pixel may differ from the transparent colour and still be cut out. Try `0` if dark parts vanish, `60`-`100` if window cut-outs stay solid |

## Output layout

```
MyTrain_glb/
  assembled_models/           <- start here: one GLB per .animated car
  loose_parts/                <- optional: individual parts, mirrors the source tree
  roblox_fix_transparency.lua <- Roblox helper (see below)
```

Without any `.animated` file, each object file becomes its own GLB.

## Coordinates

BVE is left-handed (+Z forward). The GLB is right-handed with Z mirrored, so in Blender the train points along **+Y** with **+X** to its right. Face winding is converted so normals point outwards.

## Importing into Roblox Studio

Roblox's importer ignores the alpha channel of textures, so BVE cut-outs would normally come in as solid blue (or whatever the key colour was). Two things handle it:

1. **The converter already fills cut-out pixels with the surrounding colour**, never the key colour, so windows are no longer blue even when alpha is ignored.
2. **To make them see-through**, import the GLB, select the imported model, open **View > Command Bar**, paste the contents of `roblox_fix_transparency.lua` and press Enter. It sets `SurfaceAppearance.AlphaMode` to `Transparency` on every material. Select nothing to process the whole workspace.

If a part is still odd, select it and check that `SurfaceAppearance.AlphaMode` is `Transparency` and `MeshPart.Transparency` is `0`. Roblox recommends about `0.02` for soft, semi-transparent glass.

Roblox has its own limits on mesh size, so very detailed cars may need splitting there.

## Supported

| Feature | Notes |
|---|---|
| `CreateMeshBuilder`, `[MeshBuilder]` | |
| `Vertex`/`AddVertex`, `Face`/`Face2`/`AddFace`/`AddFace2` | Quads and polygons are triangulated |
| `Cube`, `Cylinder` | |
| `Translate`, `Scale`, `Rotate`, `Shear`, `Mirror` | Plus the `*All` variants |
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
| Windows are blue or solid in Roblox | See [Importing into Roblox Studio](#importing-into-roblox-studio) |
| `no [Object]/[Include] entries found` | The `.animated` file uses an unusual layout; use `--parts` to export the parts instead |

## License

MIT. Use it, change it, share it.
