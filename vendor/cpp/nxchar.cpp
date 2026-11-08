// nxchar: dump MapleStory avatar (character look) layers for the renderer.
//
//   nxchar list <Category>            -> {"items":[{id,name,skin?},...]}
//   nxchar dump --out DIR [opts]       -> {"frames":[{"action","frame","delay","anchors","layers"}]}
//
// Categories: Skin (root 000020XX body / 000120XX head), Hair, Face, Cap, Coat,
//             Pants, Longcoat, Shoes, Glove, Cape, Shield, Weapon
//
// dump options:
//   --skin N          0..12 (body/head image id)
//   --hair ID  --face ID  --cap ID  --coat ID  --pants ID  --longcoat ID
//   --shoes ID --glove ID --cape ID --shield ID --weapon ID
//   --actions a,b     default: stand1,walk1
//   --frames 0,1,2    default: every frame the body image has
//
// This tool does no compositing: it writes one PNG per layer plus the raw
// origin/map anchors, so the Python side can order and place them exactly like
// the client does (see mxd_client_source/repo/Character/Look/*.cpp).
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

static std::string g_out;
static std::map<std::string, std::string> g_png;   // key -> png file name

static std::string esc(const std::string & s) {
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

// BGRA (NX canvas) -> RGBA PNG, rows already top-down.
static bool write_png(nl::bitmap bmp, const std::string & path) {
    if (!bmp) return false;
    const uint16_t w = bmp.width(), h = bmp.height();
    if (w == 0 || h == 0) return false;
    const auto * src = static_cast<const unsigned char *>(bmp.data());
    std::vector<unsigned char> rgba(static_cast<size_t>(w) * h * 4);
    for (size_t i = 0; i < static_cast<size_t>(w) * h; ++i) {
        rgba[i * 4 + 0] = src[i * 4 + 2];
        rgba[i * 4 + 1] = src[i * 4 + 1];
        rgba[i * 4 + 2] = src[i * 4 + 0];
        rgba[i * 4 + 3] = src[i * 4 + 3];
    }
    return stbi_write_png(path.c_str(), w, h, 4, rgba.data(), w * 4) != 0;
}

static std::string safe(const std::string & s) {
    std::string o = s;
    for (char & c : o) if (c == '/' || c == '\\' || c == ':' || c == ' ') c = '_';
    return o;
}

static std::string export_png(nl::node n, const std::string & key) {
    auto it = g_png.find(key);
    if (it != g_png.end()) return it->second;
    nl::bitmap bmp = n.get_bitmap();
    if (!bmp) { g_png[key] = ""; return ""; }

    std::string fname = safe(key) + ".png";
    fs::path p = fs::path(g_out) / "img" / fname;
    if (!write_png(bmp, p.string())) { g_png[key] = ""; return ""; }
    g_png[key] = fname;
    return fname;
}

static std::string map_json(nl::node mapn) {
    std::string o = "{";
    bool first = true;
    if (mapn) {
        for (nl::node v : mapn) {
            if (v.data_type() != nl::node::type::vector) continue;
            if (!first) o += ",";
            first = false;
            o += "\"" + esc(v.name()) + "\":[" + std::to_string(v.x()) + "," + std::to_string(v.y()) + "]";
        }
    }
    return o + "}";
}

static std::string z_of(nl::node n) {
    nl::node z = n["z"];
    if (z && z.data_type() == nl::node::type::string) return z.get_string();
    return "";
}

static void emit(std::vector<std::string> & out, const std::string & slot,
                 const std::string & part, nl::node n, const std::string & key) {
    nl::bitmap bmp = n.get_bitmap();
    if (!bmp) return;
    std::string png = export_png(n, key);
    if (png.empty()) return;

    int ox = 0, oy = 0;
    nl::node o = n["origin"];
    if (o && o.data_type() == nl::node::type::vector) { ox = o.x(); oy = o.y(); }

    std::ostringstream s;
    s << "{\"slot\":\"" << esc(slot) << "\",\"part\":\"" << esc(part) << "\""
      << ",\"z\":\"" << esc(z_of(n)) << "\""
      << ",\"img\":\"img/" << png << "\""
      << ",\"w\":" << bmp.width() << ",\"h\":" << bmp.height()
      << ",\"ox\":" << ox << ",\"oy\":" << oy
      << ",\"map\":" << map_json(n["map"]) << "}";
    out.push_back(s.str());
}

// Collects bitmap children of a frame node. Groups (e.g. hairShade with 0,1,2
// children) fall back to the image's "default" stance, exactly like Hair.cpp.
static void collect(std::vector<std::string> & out, const std::string & slot,
                    nl::node frame, const std::string & keyprefix, nl::node fallback,
                    const std::string & action, int frameidx) {
    if (!frame) return;
    for (nl::node c : frame) {
        std::string nm = c.name();
        if (nm == "map" || nm == "origin" || nm == "z" || nm == "group" ||
            nm == "delay" || nm == "face" || nm == "move" || nm == "action" ||
            nm == "frame" || nm == "info")
            continue;

        std::string key = keyprefix + "_" + action + "_" + std::to_string(frameidx) + "_" + nm;

        if (c.get_bitmap()) { emit(out, slot, nm, c, key); continue; }
        if (!fallback) continue;

        // 组节点：先去 default/<layer>，再退到 <layer>/0（hairShade 这种）
        std::string base = "default";
        if (nm.rfind("back", 0) == 0) base = "backDefault";
        nl::node d = fallback[base.c_str()][nm.c_str()];
        if (d.get_bitmap()) { emit(out, slot, nm, d, key + "_def"); continue; }
        nl::node d0 = d["0"];
        if (d0.get_bitmap()) emit(out, slot, nm, d0, key + "_def0");
    }
}

static nl::node find_part(nl::node frame, const char * name) {
    if (!frame) return {};
    for (nl::node c : frame) {
        if (std::string(c.name()) == name) return c;
        if (z_of(c) == name) return c;
    }
    return {};
}

static std::pair<int, int> anchor(nl::node frame, const char * part, const char * name) {
    nl::node p = find_part(frame, part);
    if (!p) return {0, 0};
    nl::node v = p["map"][name];
    if (v && v.data_type() == nl::node::type::vector) return {v.x(), v.y()};
    return {0, 0};
}

static std::string anchors_json(nl::node bodyFrame, nl::node headFrame) {
    auto navel   = anchor(bodyFrame, "body", "navel");
    auto neck    = anchor(bodyFrame, "body", "neck");
    auto ahand   = anchor(bodyFrame, "arm", "hand");
    auto anavel  = anchor(bodyFrame, "arm", "navel");
    auto hand2   = anchor(bodyFrame, "armOverHair", "hand");
    auto navel2  = anchor(bodyFrame, "armOverHair", "navel");
    auto move    = anchor(bodyFrame, "handBelowWeapon", "handMove");
    auto hneck   = anchor(headFrame, "head", "neck");
    auto hbrow   = anchor(headFrame, "head", "brow");

    // 手臂的 hand 是相对手臂 origin 的，换算到身体坐标系（和客户端 BodyDrawInfo 一样：
    // armPos = ARM.hand - ARM.navel + BODY.navel），武器的 hand 锚点才对得上。
    if (ahand.first == 0 && ahand.second == 0 && anavel.first == 0 && anavel.second == 0) {
        ahand = hand2;
        anavel = navel2;
    }
    int hx = ahand.first - anavel.first + navel.first;
    int hy = ahand.second - anavel.second + navel.second;

    std::ostringstream s;
    s << "{\"navel\":[" << navel.first << "," << navel.second << "]"
      << ",\"neck\":[" << neck.first << "," << neck.second << "]"
      << ",\"armHand\":[" << hx << "," << hy << "]"
      << ",\"handMove\":[" << move.first << "," << move.second << "]"
      << ",\"headNeck\":[" << hneck.first << "," << hneck.second << "]"
      << ",\"headBrow\":[" << hbrow.first << "," << hbrow.second << "]}";
    return s.str();
}

static std::string pad8(std::string id) {
    if (id.size() > 4 && id.compare(id.size() - 4, 4, ".img") == 0) id = id.substr(0, id.size() - 4);
    while (id.size() < 8) id = "0" + id;
    return id;
}

static std::vector<std::string> split(const std::string & s, char sep) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == sep) { if (!cur.empty()) out.push_back(cur); cur.clear(); }
        else cur += c;
    }
    if (!cur.empty()) out.push_back(cur);
    return out;
}

static std::string item_name(const char * cat, const std::string & id) {
    std::string num = std::to_string(std::stoll(id));   // String keys drop leading zeros
    nl::node n = nl::nx::String["Eqp.img"]["Eqp"][cat][num.c_str()]["name"];
    if (n && n.data_type() == nl::node::type::string) return n.get_string();
    return "";
}

// ---------------------------------------------------------------- list

static int cmd_list(const std::string & cat) {
    std::cout << "{\"category\":\"" << esc(cat) << "\",\"items\":[";
    bool first = true;

    if (cat == "Skin") {
        for (int skin = 0; skin <= 12; ++skin) {
            char bodyid[16], headid[16];
            std::snprintf(bodyid, sizeof(bodyid), "000020%02d.img", skin);
            std::snprintf(headid, sizeof(headid), "000120%02d.img", skin);
            if (!nl::nx::Character[bodyid] || !nl::nx::Character[headid]) continue;
            std::string nm = nl::nx::String["Eqp.img"]["Eqp"]["Skin"][std::to_string(skin).c_str()]["name"].get_string();
            if (!first) std::cout << ",";
            first = false;
            std::cout << "{\"id\":\"" << bodyid << "\",\"skin\":" << skin
                      << ",\"name\":\"" << esc(nm) << "\"}";
        }
    } else {
        nl::node dir = nl::nx::Character[cat.c_str()];
        if (!dir) { std::cerr << "no category " << cat << "\n"; return 2; }
        for (nl::node it : dir) {
            std::string nm = it.name();
            if (nm.size() < 5 || nm.compare(nm.size() - 4, 4, ".img") != 0) continue;
            std::string id = nm.substr(0, nm.size() - 4);
            if (!first) std::cout << ",";
            first = false;
            std::cout << "{\"id\":\"" << id << "\",\"name\":\"" << esc(item_name(cat.c_str(), id)) << "\"}";
        }
    }

    std::cout << "]}\n";
    return 0;
}

// ---------------------------------------------------------------- dump

struct Opts {
    std::string out;
    int skin = 0;
    std::string hair, face, cap, coat, pants, longcoat, shoes, glove, cape, shield, weapon;
    std::string actions = "stand1,walk1";
    std::string frames;
};

static std::string part_id(const Opts & o, const char * slot) {
    if (!std::strcmp(slot, "hair")) return o.hair;
    if (!std::strcmp(slot, "face")) return o.face;
    if (!std::strcmp(slot, "cap")) return o.cap;
    if (!std::strcmp(slot, "coat")) return o.coat;
    if (!std::strcmp(slot, "pants")) return o.pants;
    if (!std::strcmp(slot, "longcoat")) return o.longcoat;
    if (!std::strcmp(slot, "shoes")) return o.shoes;
    if (!std::strcmp(slot, "glove")) return o.glove;
    if (!std::strcmp(slot, "cape")) return o.cape;
    if (!std::strcmp(slot, "shield")) return o.shield;
    if (!std::strcmp(slot, "weapon")) return o.weapon;
    return "";
}

static const char * kSlots[] = { "hair", "face", "cap", "coat", "pants", "longcoat",
                                 "shoes", "glove", "cape", "shield", "weapon" };

// slot -> Character/<Category> 目录名（NX 里是首字母大写）
static const char * category_of(const std::string & slot) {
    if (slot == "hair") return "Hair";
    if (slot == "face") return "Face";
    if (slot == "cap") return "Cap";
    if (slot == "coat") return "Coat";
    if (slot == "pants") return "Pants";
    if (slot == "longcoat") return "Longcoat";
    if (slot == "shoes") return "Shoes";
    if (slot == "glove") return "Glove";
    if (slot == "cape") return "Cape";
    if (slot == "shield") return "Shield";
    if (slot == "weapon") return "Weapon";
    return "";
}

static int cmd_dump(int argc, char ** argv) {
    Opts o;
    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        std::string v = (i + 1 < argc) ? argv[i + 1] : "";
        if (k == "--out") { o.out = v; ++i; }
        else if (k == "--skin") { o.skin = std::atoi(v.c_str()); ++i; }
        else if (k == "--hair") { o.hair = v; ++i; }
        else if (k == "--face") { o.face = v; ++i; }
        else if (k == "--cap") { o.cap = v; ++i; }
        else if (k == "--coat") { o.coat = v; ++i; }
        else if (k == "--pants") { o.pants = v; ++i; }
        else if (k == "--longcoat") { o.longcoat = v; ++i; }
        else if (k == "--shoes") { o.shoes = v; ++i; }
        else if (k == "--glove") { o.glove = v; ++i; }
        else if (k == "--cape") { o.cape = v; ++i; }
        else if (k == "--shield") { o.shield = v; ++i; }
        else if (k == "--weapon") { o.weapon = v; ++i; }
        else if (k == "--actions") { o.actions = v; ++i; }
        else if (k == "--frames") { o.frames = v; ++i; }
    }
    if (o.out.empty()) { std::cerr << "--out required\n"; return 2; }

    g_out = o.out;
    fs::create_directories(fs::path(g_out) / "img");

    char bodyid[32], headid[32];
    std::snprintf(bodyid, sizeof(bodyid), "000020%02d.img", o.skin);
    std::snprintf(headid, sizeof(headid), "000120%02d.img", o.skin);
    nl::node body = nl::nx::Character[bodyid];
    nl::node head = nl::nx::Character[headid];
    if (!body) { std::cerr << "no body image " << bodyid << "\n"; return 2; }

    std::cout << "{\"body\":\"" << bodyid << "\",\"head\":\"" << headid << "\",\"frames\":[";
    bool firstFrame = true;

    for (const std::string & action : split(o.actions, ',')) {
        nl::node sn = body[action.c_str()];
        if (!sn) continue;

        std::vector<int> want;
        if (!o.frames.empty()) {
            for (const std::string & f : split(o.frames, ',')) want.push_back(std::atoi(f.c_str()));
        } else {
            for (int f = 0; sn[std::to_string(f).c_str()]; ++f) want.push_back(f);
        }

        for (int f : want) {
            nl::node bf = sn[std::to_string(f).c_str()];
            if (!bf) continue;
            nl::node hf = head[action.c_str()][std::to_string(f).c_str()];

            std::string key0 = std::string("b") + bodyid;
            std::vector<std::string> layers;
            collect(layers, "body", bf, key0 + "_body", {}, action, f);
            collect(layers, "head", hf, std::string("h") + headid + "_head", {}, action, f);

            for (const char * slot : kSlots) {
                std::string id = part_id(o, slot);
                if (id.empty()) continue;
                nl::node img = nl::nx::Character[category_of(slot)][ (pad8(id) + ".img").c_str() ];
                if (!img) continue;
                if (!std::strcmp(slot, "face")) {
                    nl::node fd = img["default"];
                    if (fd.get_bitmap())
                        emit(layers, "face", "face", fd, std::string("face") + id + "_default");
                    continue;
                }
                nl::node fr = img[action.c_str()][std::to_string(f).c_str()];
                if (!fr) continue;
                collect(layers, slot, fr, std::string(slot) + id, img, action, f);
            }

            nl::node delay = bf["delay"];
            int d = (delay && delay.data_type() == nl::node::type::integer) ? delay.get_integer() : 100;

            if (!firstFrame) std::cout << ",";
            firstFrame = false;
            std::cout << "{\"action\":\"" << esc(action) << "\",\"frame\":" << f
                      << ",\"delay\":" << d
                      << ",\"anchors\":" << anchors_json(bf, hf)
                      << ",\"layers\":[";
            for (size_t i = 0; i < layers.size(); ++i) {
                if (i) std::cout << ",";
                std::cout << layers[i];
            }
            std::cout << "]}";
        }
    }

    std::cout << "]}\n";
    return 0;
}

// ---------------------------------------------------------------- icon

// 列表缩略图：取 stand1 第 0 帧的第一个图层（脸走 default/face）
static int cmd_icon(const std::string & cat, const std::string & id, const std::string & out) {
    nl::node img = nl::nx::Character[cat.c_str()][(pad8(id) + ".img").c_str()];
    if (!img) { std::cerr << "no image " << cat << "/" << id << "\n"; return 2; }

    nl::node pick;
    nl::node fr = img["stand1"]["0"];
    if (fr) {
        for (nl::node c : fr) if (c.get_bitmap()) { pick = c; break; }
    }
    if (!pick) {
        nl::node d = img["default"];
        if (d.get_bitmap()) pick = d;
        else for (nl::node c : d) if (c.get_bitmap()) { pick = c; break; }
    }
    if (!pick) { std::cerr << "no bitmap\n"; return 3; }

    fs::path p(out);
    if (p.has_parent_path()) fs::create_directories(p.parent_path());
    return write_png(pick.get_bitmap(), out) ? 0 : 4;
}

int main(int argc, char ** argv) {
    if (argc < 2) {
        std::cerr << "usage:\n"
                     "  nxchar list <Category>\n"
                     "  nxchar icon <Category> <id> <out.png>\n"
                     "  nxchar dump --out DIR [--skin N] [--hair ID] ... [--actions a,b] [--frames 0,1]\n";
        return 1;
    }
    nl::nx::load_all();
    std::string cmd = argv[1];
    if (cmd == "list") return cmd_list(argc > 2 ? argv[2] : "");
    if (cmd == "icon" && argc >= 5) return cmd_icon(argv[2], argv[3], argv[4]);
    if (cmd == "dump") return cmd_dump(argc, argv);
    std::cerr << "unknown command " << cmd << "\n";
    return 1;
}
