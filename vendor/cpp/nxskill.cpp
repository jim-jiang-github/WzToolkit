// nxskill: browse & export skill effect animations out of Skill.nx (CN layout).
//
//   nxskill jobs                      -> {"jobs":[{"id":"110","skills":N},...]}
//   nxskill list <job>                -> {"items":[{"id","name","effects":[...],"icon":1},...]}
//   nxskill export --job 110 --skill 1101006 --out DIR
//                                     -> DIR/manifest.json + <seq>_<f>.png + icon.png
//
// Skill.nx root holds "<job>.img" volumes; each volume has skill/<skillId>/
// with effect/ball/hit/screen/keydown/... frame sequences (numeric frame
// children, each frame a bitmap or a group of bitmap parts) plus icon nodes.
// A sequence may also nest one level deeper (effect/morning/0).
//
// Must run from the directory that holds Skill.nx / String.nx.
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <stb/stb_image_write.h>

#include <nlnx/nx.hpp>
#include <nlnx/node.hpp>
#include <nlnx/bitmap.hpp>
#include <lz4.h>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <algorithm>
#include <cctype>
#include <climits>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <functional>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

// nlnx's load_all() needs Base.nx and friends; CN ships self-contained files.
namespace nl { namespace nx { node add_file(std::string name); } }

static bool load_skill() {
    if (nl::nx::Skill) return true;
    nl::node f = nl::nx::add_file("Skill.nx");
    if (f) nl::nx::Skill = f["Skill"] ? f["Skill"] : f;
    nl::node s = nl::nx::add_file("String.nx");
    if (s) nl::nx::String = s["String"] ? s["String"] : s;
    return static_cast<bool>(nl::nx::Skill);
}

static bool is_uint(const std::string & s) {
    if (s.empty()) return false;
    for (char c : s) if (!std::isdigit(static_cast<unsigned char>(c))) return false;
    return true;
}

static std::string norm_id(const std::string & s) {
    size_t p = s.find_first_not_of('0');
    if (p == std::string::npos) return "0";
    return s.substr(p);
}

static std::string esc(const std::string & s) {
    std::string out;
    for (unsigned char c : s) {
        switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) { char b[8]; std::snprintf(b, 8, "\\u%04X", c); out += b; }
            else out += static_cast<char>(c);
        }
    }
    return out;
}

// NX names are UTF-8; narrow fs::path would garble them. UTF-8 -> wide -> path.
static fs::path u8p(const std::string & s) {
#ifdef _WIN32
    if (s.empty()) return fs::path();
    int w = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring ws(static_cast<size_t>(w > 0 ? w - 1 : 0), L'\0');
    if (w > 1) MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, ws.data(), w);
    return fs::path(ws);
#else
    return fs::path(s);
#endif
}

// ------------------------------------------------------------------ frame bits

struct Src {
    int w = 0, h = 0, ox = 0, oy = 0;
    std::vector<unsigned char> own;    // decoded BGRA pixels (own storage)
    const unsigned char * pixels() const { return own.data(); }
};

static bool decode(nl::node n, Src & s) {
    nl::bitmap b = n.get_bitmap();
    if (!b) return false;
    s.w = static_cast<int>(b.width());
    s.h = static_cast<int>(b.height());
    if (!s.w || !s.h) return false;
    const char * comp = reinterpret_cast<const char *>(b.id());
    if (!comp) return false;
    uint32_t compLen = 0;
    std::memcpy(&compLen, comp, sizeof(compLen));
    if (compLen == 0 || compLen > (64u << 20)) return false;
    size_t outLen = static_cast<size_t>(s.w) * s.h * 4;
    s.own.resize(outLen);
    int r = LZ4_decompress_safe(comp + 4, reinterpret_cast<char *>(s.own.data()),
                                static_cast<int>(compLen), static_cast<int>(outLen));
    if (r < 0) return false;
    nl::node o = n["origin"];
    if (o && o.data_type() == nl::node::type::vector) { s.ox = o.x(); s.oy = o.y(); }
    return true;
}

// Bitmap parts of one frame: either the frame is the bitmap itself or its
// children are numbered/named parts (composite canvas).
static void frame_sources(nl::node fr, std::vector<nl::node> & out) {
    if (fr.data_type() == nl::node::type::bitmap) { out.push_back(fr); return; }
    std::vector<std::pair<int, std::string>> numbered;
    std::vector<std::string> named;
    for (nl::node c : fr) {
        if (c.data_type() != nl::node::type::bitmap) continue;
        std::string n = c.name();
        if (is_uint(n)) numbered.push_back({ std::atoi(n.c_str()), n });
        else named.push_back(n);
    }
    std::sort(numbered.begin(), numbered.end());
    for (auto & p : numbered) out.push_back(fr[p.second.c_str()]);
    for (auto & n : named) out.push_back(fr[n.c_str()]);
}

static bool compose(const std::vector<Src> & srcs, std::vector<unsigned char> & canvas,
                    int & W, int & H, int & ax, int & ay) {
    if (srcs.empty()) return false;
    int minx = INT_MAX, miny = INT_MAX, maxx = INT_MIN, maxy = INT_MIN;
    for (const Src & s : srcs) {
        int lx = -s.ox, ly = -s.oy;
        minx = std::min(minx, lx); miny = std::min(miny, ly);
        maxx = std::max(maxx, lx + s.w); maxy = std::max(maxy, ly + s.h);
    }
    W = maxx - minx; H = maxy - miny;
    if (W <= 0 || H <= 0 || W > 4096 || H > 4096) return false;
    ax = -minx; ay = -miny;
    canvas.assign(static_cast<size_t>(W) * H * 4, 0);
    for (const Src & s : srcs) {
        int lx = -s.ox - minx, ly = -s.oy - miny;
        for (int r = 0; r < s.h; ++r) {
            int dy = ly + r;
            if (dy < 0 || dy >= H) continue;
            for (int c = 0; c < s.w; ++c) {
                int dx = lx + c;
                if (dx < 0 || dx >= W) continue;
                const unsigned char * p = s.pixels() + (static_cast<size_t>(r) * s.w + c) * 4;
                unsigned char sa = p[3];
                if (sa == 0) continue;
                size_t di = (static_cast<size_t>(dy) * W + dx) * 4;
                unsigned char * d = canvas.data() + di;
                if (sa == 255) {
                    d[0] = p[2]; d[1] = p[1]; d[2] = p[0]; d[3] = 255;
                } else {
                    int ia = sa, inv = 255 - ia;
                    d[0] = (unsigned char)((p[2] * ia + d[0] * inv) / 255);
                    d[1] = (unsigned char)((p[1] * ia + d[1] * inv) / 255);
                    d[2] = (unsigned char)((p[0] * ia + d[2] * inv) / 255);
                    d[3] = (unsigned char)(ia + (d[3] * inv) / 255);
                }
            }
        }
    }
    return true;
}

static bool write_rgba_png(const std::string & path, int W, int H,
                           const std::vector<unsigned char> & bgra) {
    std::vector<unsigned char> rgba(bgra.size());
    for (size_t i = 0; i < bgra.size(); i += 4) {
        rgba[i + 0] = bgra[i + 2];
        rgba[i + 1] = bgra[i + 1];
        rgba[i + 2] = bgra[i + 0];
        rgba[i + 3] = bgra[i + 3];
    }
    return stbi_write_png(path.c_str(), W, H, 4, rgba.data(), W * 4) != 0;
}

// Exports one bitmap node (single canvas, no parts) to PNG. Returns true if written.
static bool export_single(nl::node n, const fs::path & file) {
    Src s;
    if (!decode(n, s)) return false;
    std::vector<unsigned char> rgba(static_cast<size_t>(s.w) * s.h * 4);
    for (size_t i = 0; i < rgba.size(); i += 4) {
        rgba[i + 0] = s.own[i + 2];
        rgba[i + 1] = s.own[i + 1];
        rgba[i + 2] = s.own[i + 0];
        rgba[i + 3] = s.own[i + 3];
    }
    return stbi_write_png(u8p(file.string()).string().c_str(), s.w, s.h, 4,
                          rgba.data(), s.w * 4) != 0;
}

// ------------------------------------------------------------------- commands

// 帧号不一定从 0 开始连续：枚举数字名子节点并按数值排序（跳过非帧子树）。
static std::vector<int> numeric_frame_ids(nl::node n) {
    std::vector<int> fids;
    if (!n) return fids;
    for (nl::node ch : n) {
        std::string nm = ch.name();
        bool numeric = !nm.empty();
        for (char c : nm)
            if (!std::isdigit(static_cast<unsigned char>(c))) { numeric = false; break; }
        if (numeric) fids.push_back(std::atoi(nm.c_str()));
    }
    std::sort(fids.begin(), fids.end());
    return fids;
}

// True if node looks like a frame sequence: numeric children holding bitmaps.
static bool is_sequence(nl::node n, int & frames) {
    frames = 0;
    bool any = false;
    for (int f : numeric_frame_ids(n)) {
        nl::node fr = n[std::to_string(f).c_str()];
        std::vector<nl::node> srcs;
        frame_sources(fr, srcs);
        if (srcs.empty()) continue;      // frame without pixels: skip but keep order
        ++frames;
        any = true;
    }
    return any;
}

static std::string skill_name(const std::string & id) {
    nl::node n = nl::nx::String["Skill.img"][id.c_str()]["name"];
    if (!n) return std::string();
    return n.get_string();
}

// Job volume lookup: "<job>.img" at the root, or one level down inside a
// named mount subtree (MobSkill/, Dragon/, ... from named --mount volumes).
static nl::node find_job(const std::string & job) {
    nl::node n = nl::nx::Skill[(job + ".img").c_str()];
    if (n) return n;
    for (nl::node dir : nl::nx::Skill) {
        std::string dn = dir.name();
        if (dn.size() > 4 && dn.compare(dn.size() - 4, 4, ".img") == 0) continue;
        nl::node v = dir[(job + ".img").c_str()];
        if (v) return v;
    }
    return nl::node();
}

// Every job volume: root-level "<X>.img" plus "<dir>/<X>.img" (named mounts).
static void each_job(const std::function<void(const std::string &, nl::node)> & fn) {
    std::vector<std::pair<std::string, nl::node>> out;
    auto add = [&](const std::string & id, nl::node n) {
        for (auto & p : out) if (p.first == id) return;
        out.push_back({ id, n });
    };
    for (nl::node c : nl::nx::Skill) {
        std::string n = c.name();
        if (n.size() > 4 && n.compare(n.size() - 4, 4, ".img") == 0)
            add(n.substr(0, n.size() - 4), c);
        else
            for (nl::node v : c) {
                std::string vn = v.name();
                if (vn.size() > 4 && vn.compare(vn.size() - 4, 4, ".img") == 0)
                    add(vn.substr(0, vn.size() - 4), v);
            }
    }
    std::sort(out.begin(), out.end(), [](const auto & a, const auto & b) {
        if (a.first.size() != b.first.size()) return a.first.size() < b.first.size();
        return a.first < b.first;
    });
    for (auto & p : out) fn(p.first, p.second);
}

static int cmd_jobs() {
    std::vector<std::pair<std::string, int>> jobs;   // id, skill count
    each_job([&](const std::string & id, nl::node c) {
        int count = 0;
        nl::node sk = c["skill"];
        if (sk) for (nl::node s : sk) {
            std::string sn = s.name();
            if (is_uint(sn)) ++count;
        }
        jobs.push_back({ id, count });
    });
    std::cout << "{\"jobs\":[";
    bool first = true;
    for (auto & j : jobs) {
        if (!first) std::cout << ",";
        first = false;
        std::cout << "{\"id\":\"" << j.first << "\",\"skills\":" << j.second << "}";
    }
    std::cout << "]}\n";
    return 0;
}

static int cmd_list(const std::string & job) {
    nl::node vol = find_job(job);
    nl::node sk = vol ? vol["skill"] : nl::node();
    if (!sk) { std::cout << "{\"items\":[]}\n"; return 0; }
    std::cout << "{\"job\":\"" << job << "\",\"items\":[";
    bool first = true;
    for (nl::node s : sk) {
        std::string id = s.name();
        if (!is_uint(id)) continue;
        std::vector<std::string> effects;
        bool icon = false;
        int frames = 0;
        for (nl::node c : s) {
            std::string cn = c.name();
            if (cn == "icon" || cn == "iconMouseOver" || cn == "iconDisabled") { icon = true; continue; }
            if (is_sequence(c, frames))
                effects.push_back(cn);
            else {
                // one nested level (effect/morning/0)
                int f2 = 0;
                std::vector<std::string> subs;
                for (nl::node g : c) if (is_sequence(g, f2)) subs.push_back(g.name());
                if (!subs.empty()) effects.push_back(cn + "/" + subs[0]);
            }
        }
        if (!first) std::cout << ",";
        first = false;
        std::cout << "{\"id\":\"" << id << "\",\"name\":\"" << esc(skill_name(id))
                  << "\",\"icon\":" << (icon ? 1 : 0) << ",\"effects\":[";
        for (size_t i = 0; i < effects.size(); ++i) {
            if (i) std::cout << ",";
            std::cout << "\"" << effects[i] << "\"";
        }
        std::cout << "]}";
    }
    std::cout << "]}\n";
    return 0;
}

static int cmd_export(const std::string & job, const std::string & skill,
                      const std::string & outdir) {
    nl::node vol = find_job(job);
    nl::node s = vol ? vol["skill"][skill.c_str()] : nl::node();
    if (!s) { std::cerr << "skill not found: " << job << "/" << skill << "\n"; return 1; }
    fs::create_directories(outdir);

    std::ostringstream man;
    man << "{\"job\":\"" << job << "\",\"id\":\"" << skill << "\",\"name\":\""
        << esc(skill_name(skill)) << "\",\"seqs\":[";
    bool firstSeq = true;

    // Frame sequences (two nesting levels: effect/0 and effect/morning/0).
    for (nl::node c : s) {
        std::string cn = c.name();
        if (cn == "icon" || cn == "iconMouseOver" || cn == "iconDisabled") continue;
        struct Entry { std::string path; std::string label; nl::node node; };
        std::vector<Entry> seqs;
        int frames = 0;
        if (is_sequence(c, frames)) {
            seqs.push_back({ cn, cn, c });
        } else {
            for (nl::node g : c) {
                if (is_sequence(g, frames))
                    seqs.push_back({ cn + "/" + g.name(), cn + "_" + g.name(), g });
            }
        }
        for (const Entry & e : seqs) {
            std::vector<std::string> pngs;
            std::vector<long long> delays;
            std::vector<int> axs, ays, ws, hs;
            std::string base = e.label;
            std::replace(base.begin(), base.end(), '/', '_');
            for (int f : numeric_frame_ids(e.node)) {
                nl::node fr = e.node[std::to_string(f).c_str()];
                std::vector<nl::node> srcs;
                frame_sources(fr, srcs);
                if (srcs.empty()) continue;
                std::vector<Src> decoded;
                bool ok = true;
                for (nl::node p : srcs) {
                    Src d;
                    if (!decode(p, d)) { ok = false; break; }
                    decoded.push_back(d);
                }
                if (!ok || decoded.empty()) continue;
                std::vector<unsigned char> canvas;
                int W, H, ax, ay;
                if (!compose(decoded, canvas, W, H, ax, ay)) continue;
                std::string fname = base + "_" + std::to_string(f) + ".png";
                if (!write_rgba_png(u8p((fs::path(outdir) / fname).string()).string(), W, H, canvas))
                    continue;
                pngs.push_back(fname);
                nl::node dl = fr["delay"];
                long long delay = 0;
                if (dl && dl.data_type() == nl::node::type::integer) delay = dl.get_integer();
                delays.push_back(delay);
                axs.push_back(ax); ays.push_back(ay);
                ws.push_back(W); hs.push_back(H);
            }
            if (pngs.empty()) continue;
            if (!firstSeq) man << ",";
            firstSeq = false;
            man << "{\"name\":\"" << e.path << "\",\"frames\":[";
            for (size_t i = 0; i < pngs.size(); ++i) {
                if (i) man << ",";
                man << "{\"img\":\"" << pngs[i] << "\",\"delay\":" << delays[i]
                    << ",\"ax\":" << axs[i] << ",\"ay\":" << ays[i]
                    << ",\"w\":" << ws[i] << ",\"h\":" << hs[i] << "}";
            }
            man << "]}";
        }
    }
    man << "]";

    // Icons (single bitmaps).
    std::vector<std::string> icons;
    for (const char * iname : { "icon", "iconMouseOver", "iconDisabled" }) {
        nl::node ic = s[iname];
        if (!ic || ic.data_type() != nl::node::type::bitmap) continue;
        std::string fname = std::string(iname) + ".png";
        if (export_single(ic, fs::path(outdir) / fname))
            icons.push_back(fname);
    }
    man << ",\"icons\":[";
    for (size_t i = 0; i < icons.size(); ++i) {
        if (i) man << ",";
        man << "\"" << icons[i] << "\"";
    }
    man << "]}\n";

    std::ofstream f(fs::path(outdir) / "manifest.json", std::ios::binary);
    std::string str = man.str();
    f.write(str.data(), static_cast<std::streamsize>(str.size()));
    std::cout << "{\"ok\":true,\"dir\":\"" << outdir << "\"}\n";
    return 0;
}

static int usage() {
    std::fprintf(stderr,
        "nxskill: skill effect exporter\n"
        "  nxskill jobs\n"
        "  nxskill list <job>\n"
        "  nxskill export --job <job> --skill <id> --out DIR\n");
    return 2;
}

int main(int argc, char ** argv) {
    std::vector<std::string> args(argv + (argc > 0 ? 1 : 0), argv + argc);
    if (args.empty()) return usage();
    if (!load_skill()) { std::cerr << "Skill.nx not found (run from .nx dir)\n"; return 1; }
    if (args[0] == "jobs") return cmd_jobs();
    if (args[0] == "list" && args.size() > 1) return cmd_list(args[1]);
    if (args[0] == "export") {
        std::string job, skill, out;
        for (size_t i = 1; i + 1 < args.size(); i += 2) {
            if (args[i] == "--job") job = args[i + 1];
            else if (args[i] == "--skill") skill = args[i + 1];
            else if (args[i] == "--out") out = args[i + 1];
        }
        if (job.empty() || skill.empty() || out.empty()) return usage();
        return cmd_export(job, skill, out);
    }
    return usage();
}
