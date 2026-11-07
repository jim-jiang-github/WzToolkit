// nxmap: export one NX map into a format the Godot client can consume directly.
//
//   nxmap.exe <mapid> <outdir>
//
// Writes:
//   <outdir>/map.json          layer/tile/obj/back/foothold/portal/life metadata
//   <outdir>/img/<key>.png     every bitmap the map references, deduplicated
//
// Bitmaps are decoded through NoLifeNx (which owns the LZ4 + BGRA details) and
// re-encoded as PNG so the Godot side never has to touch compression formats.
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <stb/stb_image_write.h>

#include <nlnx/nx.hpp>
#include <nlnx/node.hpp>
#include <nlnx/bitmap.hpp>

#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

static std::string g_outdir;
static std::map<std::string, std::string> g_img_done;  // key -> png filename
static int g_img_written = 0;

static std::string json_escape(const std::string & s) {
    std::string out;
    for (unsigned char c : s) {
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) { char b[8]; std::snprintf(b, sizeof(b), "\\u%04x", c); out += b; }
            else out += static_cast<char>(c);
        }
    }
    return out;
}

// Converts BGRA into RGBA and writes a PNG.
// NX (converted by NoLifeWzToNx) already stores rows top-down, so no flip here.
static bool write_png(nl::bitmap bmp, const std::string & path) {
    if (!bmp) return false;
    const uint16_t w = bmp.width(), h = bmp.height();
    if (w == 0 || h == 0) return false;
    const auto * src = static_cast<const unsigned char *>(bmp.data());
    std::vector<unsigned char> rgba(static_cast<size_t>(w) * h * 4);
    for (uint32_t y = 0; y < h; ++y) {
        for (uint32_t x = 0; x < w; ++x) {
            const size_t si = (static_cast<size_t>(y) * w + x) * 4;
            const size_t di = (static_cast<size_t>(y) * w + x) * 4;
            rgba[di + 0] = src[si + 2];  // R
            rgba[di + 1] = src[si + 1];  // G
            rgba[di + 2] = src[si + 0];  // B
            rgba[di + 3] = src[si + 3];  // A  (NX canvas is premultiplied-ish BGRA)
        }
    }
    return stbi_write_png(path.c_str(), w, h, 4, rgba.data(), w * 4) != 0;
}

// NX directories carry the ".img" suffix ("grassySoil.img"), while the map's
// bS/u/oS fields hold the bare name ("grassySoil"). Normalise here so callers
// never have to remember.
static std::string img_name(const std::string & bare) {
    if (bare.size() > 4 && bare.compare(bare.size() - 4, 4, ".img") == 0) return bare;
    return bare + ".img";
}

// Returns the PNG filename for a bitmap node, exporting it once.
static std::string export_bitmap(nl::node n, const std::string & key) {
    auto it = g_img_done.find(key);
    if (it != g_img_done.end()) return it->second;

    nl::bitmap bmp = n.get_bitmap();
    if (!bmp) { g_img_done[key] = ""; return ""; }

    std::string fname = key + ".png";
    for (char & c : fname) if (c == '/' || c == '\\' || c == ':') c = '_';
    fs::path dir = fs::path(g_outdir) / "img";
    fs::create_directories(dir);
    if (!write_png(bmp, (dir / fname).string())) { g_img_done[key] = ""; return ""; }
    ++g_img_written;
    g_img_done[key] = fname;
    return fname;
}

static std::string jget_str(nl::node n, const char * k, const std::string & def = "") {
    nl::node c = n[k];
    if (!c || c.data_type() != nl::node::type::string) return def;
    return c.get_string();
}
static int64_t jget_int(nl::node n, const char * k, int64_t def = 0) {
    nl::node c = n[k];
    if (!c) return def;
    // get_integer() on a string/bitmap node aborts inside NoLifeNx, so gate on
    // the type rather than trusting the caller.
    if (c.data_type() != nl::node::type::integer && c.data_type() != nl::node::type::real) return def;
    return c.get_integer();
}

// ---------------------------------------------------------------- 地图名缓存
// String/Map.img/<分类>/<mapid>/{mapName,streetName}。小地图标题栏和传送门
// tooltip 都要用（客户端 UIMiniMap::update_text 读的是同一份数据）。
struct MapName { std::string name, street; };
static std::map<std::string, MapName> g_map_names;
static bool g_map_names_loaded = false;

static void load_map_names() {
    if (g_map_names_loaded) return;
    g_map_names_loaded = true;
    if (!nl::nx::String) return;
    nl::node strMap = nl::nx::String["Map.img"];
    for (auto cat = strMap.begin(); cat != strMap.end(); ++cat) {
        nl::node c = *cat;
        for (auto mi = c.begin(); mi != c.end(); ++mi) {
            nl::node m = *mi;
            std::string id = m.name();
            if (id.size() > 4 && id.compare(id.size() - 4, 4, ".img") == 0)
                id = id.substr(0, id.size() - 4);
            g_map_names[id] = MapName{jget_str(m, "mapName"), jget_str(m, "streetName")};
        }
    }
}

static const MapName & map_name_of(const std::string & id) {
    static const MapName empty;
    load_map_names();
    auto it = g_map_names.find(id);
    return it == g_map_names.end() ? empty : it->second;
}

// ---------------------------------------------------------------- life 全库导出
// 每张图的 life 节点就是官方刷怪表：怪物 id + 出生坐标 + 所站地面 + 重生时间。
// 反向聚合即可得到「怪物 → 出现地图」对照（boss 的 mobTime > 0 是重生秒数）。
static std::string csv_esc(const std::string & s) {
    if (s.find(',') == std::string::npos && s.find('"') == std::string::npos &&
        s.find('\n') == std::string::npos) return s;
    std::string out = "\"";
    for (char c : s) { out += c; if (c == '"') out += '"'; }
    return out + "\"";
}

static int cmd_life() {
    nl::nx::load_all();
    // String/Map.img/<分类>/<mapid>/mapName —— 分类一层（elin、etc、Episode1GL…）
    std::map<std::string, std::string> map_names;
    if (nl::nx::String) {
        nl::node strMap = nl::nx::String["Map.img"];
        for (auto cat = strMap.begin(); cat != strMap.end(); ++cat) {
            for (auto mi = (*cat).begin(); mi != (*cat).end(); ++mi) {
                nl::node mn = (*mi)["mapName"];
                if (mn && mn.data_type() == nl::node::type::string)
                    map_names[(*mi).name()] = mn.get_string();
            }
        }
    }
    std::cout << "\xEF\xBB\xBF" << "mapid,mapname,type,id,x,y,fh,mobtime\n";
    int maps = 0, rows = 0;
    for (const char * shard : {"Map0","Map1","Map2","Map3","Map4","Map5","Map6","Map7","Map8","Map9"}) {
        nl::node sh = nl::nx::Map["Map"][shard];
        if (!sh) continue;
        for (auto it = sh.begin(); it != sh.end(); ++it) {
            nl::node img = *it;
            std::string id = img.name();
            if (id.size() > 4 && id.compare(id.size() - 4, 4, ".img") == 0)
                id = id.substr(0, id.size() - 4);
            nl::node lifeRoot = img["life"];
            if (!lifeRoot) continue;
            ++maps;
            auto found = map_names.find(id);
            std::string mapname = (found != map_names.end()) ? found->second : std::string();
            for (auto li = lifeRoot.begin(); li != lifeRoot.end(); ++li) {
                nl::node l = *li;
                std::string type = jget_str(l, "type");
                if (type != "m" && type != "n") continue;  // 只关心怪和 NPC
                std::cout << csv_esc(id) << "," << csv_esc(mapname) << "," << type
                          << "," << csv_esc(jget_str(l, "id"))
                          << "," << jget_int(l, "x") << "," << jget_int(l, "y")
                          << "," << jget_int(l, "fh") << "," << jget_int(l, "mobTime")
                          << "\n";
                ++rows;
            }
        }
    }
    std::cerr << "maps with life: " << maps << ", life rows: " << rows << "\n";
    return 0;
}

int main(int argc, char ** argv) {
    if (argc >= 2 && std::strcmp(argv[1], "life") == 0)
        return cmd_life();
    if (argc < 3) {
        std::cerr << "usage:\n"
                     "  nxmap <mapid> <outdir>\n"
                     "  nxmap life        # 全库刷怪表 CSV（mapid,mapname,type,id,x,y,fh,mobtime）\n";
        return 1;
    }
    const std::string mapid = argv[1];
    g_outdir = argv[2];
    fs::create_directories(g_outdir);

    nl::nx::load_all();

    // Map ids are split across Map/Map0..Map9 by leading digits.
    std::string mapdir = (mapid.size() >= 2 && mapid[0] == '1' && mapid[1] == '0') ? "Map1" : "Map0";
    if (mapid.size() >= 3) {
        // HeavenClient resolves shard by the first three digits; mirror the common cases.
        const std::string p3 = mapid.substr(0, 3);
        if (p3 == "100" || p3 == "101" || p3 == "102" || p3 == "103" || p3 == "104") mapdir = "Map1";
        else if (p3 >= "200" && p3 < "300") mapdir = "Map2";
        else if (p3 >= "300" && p3 < "400") mapdir = "Map3";
        else if (p3 >= "500" && p3 < "600") mapdir = "Map5";
        else if (p3 >= "600" && p3 < "700") mapdir = "Map6";
        else if (p3 >= "700" && p3 < "800") mapdir = "Map7";
        else if (p3 >= "800" && p3 < "900") mapdir = "Map8";
        else if (p3 >= "900") mapdir = "Map9";
        else mapdir = "Map0";
    }
    (void)mapdir;

    nl::node img;
    for (const char * shard : {"Map0","Map1","Map2","Map3","Map4","Map5","Map6","Map7","Map8","Map9"}) {
        // Layout is Map/Map/<shard>/<id>.img - note the doubled "Map".
        nl::node cand = nl::nx::Map["Map"][shard][(mapid + ".img").c_str()];
        if (cand) { img = cand; break; }
    }
    if (!img) { std::cerr << "map " << mapid << " not found\n"; return 2; }

    std::ostringstream meta;
    meta << "{\n";
    // id 必须写成字符串：有些地图 id 带前导零（000000001、001000005…），
    // 直接当数字输出就是 000000001 → 非法 JSON，整个 map.json 解析失败。
    meta << "  \"id\": \"" << json_escape(mapid) << "\",\n";
    const MapName & mn = map_name_of(mapid);
    meta << "  \"name\": \"" << json_escape(mn.name) << "\",\n";
    meta << "  \"streetName\": \"" << json_escape(mn.street) << "\",\n";

    nl::node info = img["info"];
    meta << "  \"info\": {";
    if (info) {
        bool first = true;
        for (auto it = info.begin(); it != info.end(); ++it) {
            nl::node c = *it;
            if (!first) meta << ",";
            first = false;
            meta << "\n    \"" << json_escape(c.name()) << "\": ";
            switch (c.data_type()) {
            case nl::node::type::integer: meta << c.get_integer(); break;
            case nl::node::type::real:    meta << c.get_real(); break;
            case nl::node::type::string:  meta << "\"" << json_escape(c.get_string()) << "\""; break;
            default: meta << "null"; break;
            }
        }
        meta << "\n  ";
    }
    meta << "},\n";

    // ---- backgrounds ----
    meta << "  \"back\": [";
    {
        bool first = true;
        nl::node back = img["back"];
        for (auto it = back.begin(); it != back.end(); ++it) {
            nl::node b = *it;
            std::string bs = jget_str(b, "bS");
            int no = static_cast<int>(jget_int(b, "no", 0));
            std::string key = "Back/" + bs + "/back/" + std::to_string(no);
            nl::node bmpnode = nl::nx::Map["Back"][img_name(bs).c_str()]["back"][std::to_string(no).c_str()];
            std::string png = bmpnode ? export_bitmap(bmpnode, key) : "";
            // Animated backgrounds keep frames under "ani"; grab frame 0 for now.
            if (png.empty() && jget_int(b, "ani", 0) != 0) {
                std::string k0 = key + "/0";
                nl::node a0 = nl::nx::Map["Back"][img_name(bs).c_str()]["ani"][std::to_string(no).c_str()]["0"];
                if (a0) png = export_bitmap(a0, k0);
            }
            if (!first) meta << ",";
            first = false;
            meta << "\n    {\"bS\":\"" << json_escape(bs) << "\",\"no\":" << no
                 << ",\"x\":" << jget_int(b, "x") << ",\"y\":" << jget_int(b, "y")
                 << ",\"rx\":" << jget_int(b, "rx") << ",\"ry\":" << jget_int(b, "ry")
                 << ",\"type\":" << jget_int(b, "type") << ",\"a\":" << jget_int(b, "a", 255)
                 << ",\"front\":" << jget_int(b, "front") << ",\"ani\":" << jget_int(b, "ani")
                 << ",\"f\":" << jget_int(b, "f") << ",\"cx\":" << jget_int(b, "cx")
                 << ",\"cy\":" << jget_int(b, "cy")
                 << ",\"img\":\"" << json_escape(png) << "\"}";
        }
        meta << "\n  ],\n";
    }

    // ---- layers: tile + obj, each with its own z ordering ----
    meta << "  \"layers\": [";
    for (int li = 0; li < 8; ++li) {
        nl::node layer = img[std::to_string(li).c_str()];
        if (li) meta << ",";
        meta << "\n    {\"index\":" << li << ",\"tile\":[";
        bool firstT = true;
        // Bind the sub-node to a named local first. Calling begin()/end() on two
        // separate temporaries from operator[] yields dangling iterators.
        nl::node tileRoot = layer["tile"];
        for (auto it = tileRoot.begin(); it != tileRoot.end(); ++it) {
            nl::node t = *it;
            std::string u = jget_str(t, "u");
            int no = static_cast<int>(jget_int(t, "no", 0));
            std::string key = "Tile/" + u + "/" + std::to_string(no);
            // Tile layout: Tile/<tS>.img/<u>/<no>: the set name is layer info/tS; — u (edU, enH0, ...) is a
            // subfolder inside the tile-set image, NOT an image itself.
            std::string ts = jget_str(layer["info"], "tS");
            nl::node bmpnode = nl::nx::Map["Tile"][img_name(ts).c_str()][u.c_str()][std::to_string(no).c_str()];
            std::string png = bmpnode ? export_bitmap(bmpnode, key) : "";
            nl::node origin = bmpnode["origin"];
            int ox = origin ? origin.x() : 0;
            int oy = origin ? origin.y() : 0;
            if (!firstT) meta << ",";
            firstT = false;
            meta << "\n      {\"u\":\"" << json_escape(u) << "\",\"no\":" << no
                 << ",\"x\":" << jget_int(t, "x") << ",\"y\":" << jget_int(t, "y")
                 << ",\"z\":" << jget_int(t, "zM", jget_int(t, "z"))
                 << ",\"ox\":" << ox << ",\"oy\":" << oy
                 << ",\"img\":\"" << json_escape(png) << "\"}";
        }
        meta << "\n    ],\"obj\":[";
        bool firstO = true;
        nl::node objRoot = layer["obj"];
        for (auto it = objRoot.begin(); it != objRoot.end(); ++it) {
            nl::node o = *it;
            std::string os = jget_str(o, "oS");
            std::string l0 = jget_str(o, "l0");
            std::string l1 = jget_str(o, "l1");
            std::string l2 = jget_str(o, "l2");
            int no = static_cast<int>(jget_int(o, "no", 0));
            std::string key = "Obj/" + os + "/" + l0 + "/" + l1 + "/" + l2 + "/" + std::to_string(no);
            // l2 is empty for many objects; indexing with "" lands on nothing.
            nl::node objNode = nl::nx::Map["Obj"][img_name(os).c_str()][l0.c_str()][l1.c_str()];
            if (!l2.empty()) objNode = objNode[l2.c_str()];
            nl::node bmpnode = objNode[std::to_string(no).c_str()];
            std::string png = bmpnode ? export_bitmap(bmpnode, key) : "";
            if (png.empty()) {
                // Animated objs store frames under ani/0..N; use frame 0.
                nl::node a0 = objNode["ani"]["0"];
                if (a0) png = export_bitmap(a0, key + "/ani0");
            }
            nl::node origin = bmpnode["origin"];
            int ox = origin ? origin.x() : 0;
            int oy = origin ? origin.y() : 0;
            if (!firstO) meta << ",";
            firstO = false;
            meta << "\n      {\"oS\":\"" << json_escape(os) << "\",\"l0\":\"" << json_escape(l0)
                 << "\",\"l1\":\"" << json_escape(l1) << "\",\"l2\":\"" << json_escape(l2)
                 << "\",\"no\":" << no
                 << ",\"x\":" << jget_int(o, "x") << ",\"y\":" << jget_int(o, "y")
                 << ",\"z\":" << jget_int(o, "z", jget_int(o, "zM"))
                 << ",\"f\":" << jget_int(o, "f")
                 << ",\"ox\":" << ox << ",\"oy\":" << oy
                 << ",\"img\":\"" << json_escape(png) << "\"}";
        }
        meta << "\n    ]}";
    }
    meta << "\n  ],\n";

    // ---- footholds (walkable ground segments) ----
    meta << "  \"foothold\": [";
    {
        bool first = true;
        nl::node fh = img["foothold"];
        // Note the three levels: foothold/<layer>/<group>/<seg>. Level 2 is a
        // grouping bucket, not a segment, so a two-level walk reads the wrong
        // node and then indexes into garbage. The layer is the bucket name
        // (level 1), segments have no "layer" child of their own.
        for (auto a = fh.begin(); a != fh.end(); ++a) {
            int layer = atoi(a.name().c_str());
            std::cerr << "[fh] bucket '" << a.name() << "' -> layer " << layer << std::endl;
            nl::node lvl = *a;
            for (auto b = lvl.begin(); b != lvl.end(); ++b) {
                nl::node segs = *b;
                for (auto c = segs.begin(); c != segs.end(); ++c) {
                    nl::node seg = *c;
                    if (!first) meta << ",";
                    first = false;
                    meta << "\n    {\"id\":" << atoi(c.name().c_str())
                         << ",\"x1\":" << jget_int(seg, "x1") << ",\"y1\":" << jget_int(seg, "y1")
                         << ",\"x2\":" << jget_int(seg, "x2") << ",\"y2\":" << jget_int(seg, "y2")
                         << ",\"prev\":" << jget_int(seg, "prev") << ",\"next\":" << jget_int(seg, "next")
                         << ",\"layer\":" << layer << "}";
                }
            }
        }
        meta << "\n  ],\n";
    }

    // ---- ladders / ropes (MapInfo::ladders, Ladder::Ladder) ----
    // l = true 表示梯子（ladder），false 是绳子（rope）；决定攀爬姿势用哪个 stance。
    meta << "  \"ladderRope\": [";
    {
        bool first = true;
        for (auto lr : img["ladderRope"]) {
            if (!first) meta << ",";
            first = false;
            meta << "\n    {\"x\":" << jget_int(lr, "x") << ",\"y1\":" << jget_int(lr, "y1")
                 << ",\"y2\":" << jget_int(lr, "y2") << ",\"l\":" << (lr["l"].get_bool() ? 1 : 0) << "}";
        }
        meta << "\n  ],\n";
    }

    // ---- portals ----
    meta << "  \"portal\": [";
    {
        bool first = true;
        nl::node portalRoot = img["portal"];
        for (auto it = portalRoot.begin(); it != portalRoot.end(); ++it) {
            nl::node p = *it;
            if (!first) meta << ",";
            first = false;
            // tm 是目标地图 id：客户端在小地图上悬停传送门时显示它的 mapName，
            // 这里直接把名字查好，前端就不用再问一次 String.nx。
            const int64_t tm = jget_int(p, "tm");
            std::string tmn;
            if (tm > 0) {
                char buf[16];
                std::snprintf(buf, sizeof(buf), "%09lld", static_cast<long long>(tm));
                tmn = map_name_of(buf).name;
            }
            meta << "\n    {\"pn\":\"" << json_escape(jget_str(p, "pn")) << "\",\"pt\":" << jget_int(p, "pt")
                 << ",\"x\":" << jget_int(p, "x") << ",\"y\":" << jget_int(p, "y")
                 << ",\"tm\":" << tm << ",\"tn\":\"" << json_escape(jget_str(p, "tn")) << "\""
                 << ",\"tmn\":\"" << json_escape(tmn) << "\""
                 << ",\"index\":" << json_escape(p.name()) << "}";
        }
        meta << "\n  ],\n";
    }

    // ---- miniMap（客户端 UIMiniMap 用的那份预烘焙小图 + 坐标换算参数） ----
    // canvas 是 WZ 里已经缩好的小图；真实地图坐标 p 映射到画布像素的规则是
    //   px = (p.x + centerX) / 2^mag,  py = (p.y + centerY) / 2^mag
    // 与 UIMiniMap::draw_movable_markers / update_static_markers 完全一致。
    meta << "  \"miniMap\": ";
    {
        nl::node mm = img["miniMap"];
        // 不是所有图都有 miniMap（hideMinimap / 部分副本图），先判空再取下标，
        // 否则 NoLifeNx 会在无效节点上 abort。
        nl::node canvas = mm ? mm["canvas"] : nl::node();
        std::string png = canvas ? export_bitmap(canvas, "minimap") : "";
        if (!mm || png.empty()) {
            meta << "null";
        } else {
            nl::bitmap bmp = canvas.get_bitmap();
            int64_t w = jget_int(mm, "width");
            int64_t h = jget_int(mm, "height");
            if (w <= 0) w = bmp ? bmp.width() : 0;
            if (h <= 0) h = bmp ? bmp.height() : 0;
            meta << "{\"img\":\"" << json_escape(png) << "\""
                 << ",\"w\":" << w << ",\"h\":" << h
                 << ",\"cw\":" << (bmp ? bmp.width() : 0)
                 << ",\"ch\":" << (bmp ? bmp.height() : 0)
                 << ",\"centerX\":" << jget_int(mm, "centerX")
                 << ",\"centerY\":" << jget_int(mm, "centerY")
                 << ",\"mag\":" << jget_int(mm, "mag") << "}";
        }
    }
    meta << ",\n";

    // ---- life (npcs/mobs placed on the map) ----
    meta << "  \"life\": [";
    {
        bool first = true;
        nl::node lifeRoot = img["life"];
        for (auto it = lifeRoot.begin(); it != lifeRoot.end(); ++it) {
            nl::node l = *it;
            if (!first) meta << ",";
            first = false;
            meta << "\n    {\"type\":\"" << json_escape(jget_str(l, "type")) << "\""
                 << ",\"id\":\"" << json_escape(jget_str(l, "id")) << "\""
                 << ",\"x\":" << jget_int(l, "x") << ",\"y\":" << jget_int(l, "y")
                 << ",\"cy\":" << jget_int(l, "cy") << ",\"fh\":" << jget_int(l, "fh")
                 << ",\"rx0\":" << jget_int(l, "rx0") << ",\"rx1\":" << jget_int(l, "rx1")
                 << ",\"mobTime\":" << jget_int(l, "mobTime") << "}";
        }
        meta << "\n  ]\n";
    }
    meta << "}\n";

    std::ofstream f(fs::path(g_outdir) / "map.json");
    f << meta.str();
    f.close();

    std::cout << "map " << mapid << " -> " << g_outdir
              << "  bitmaps=" << g_img_written
              << "  json=" << meta.str().size() << "B\n";
    return 0;
}
