// nxmob: bulk-export monster sprites out of Mob.nx (+ Mob001/002/2 volumes).
//
//   nxmob count                       -- survey: how many mobs / anims / frames
//   nxmob export <outdir> [options]   -- write one PNG per animation frame
//
// Options:
//   --stand-only     only the "stand" animation (one representative pose set)
//   --first-frame    only frame 0 of every animation
//   --flat           <outdir>/<id>_<anim>_<f>.png instead of <outdir>/<id>/...
//   --no-composite   write every body part separately instead of merging a frame
//   --named          folder name becomes "<id>_<name>" (name from String.nx)
//
// Must run from the directory that holds the .nx files (NoLifeNx resolves cwd).
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
#include <psapi.h>

#include <algorithm>
#include <cctype>
#include <climits>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

// ------------------------------------------------------------------ node utils

static bool is_uint(const std::string & s) {
    if (s.empty()) return false;
    for (char c : s) if (!std::isdigit(static_cast<unsigned char>(c))) return false;
    return true;
}

// Mob.nx holds "<id>.img" nodes at the root; v83 also ships Mob001/Mob002/Mob2
// volumes, which may repeat ids -- keep the first hit only.
static std::vector<nl::node> g_roots;

static bool collect_roots() {
    g_roots.clear();
    nl::node r[4] = { nl::nx::Mob, nl::nx::Mob001, nl::nx::Mob002, nl::nx::Mob2 };
    for (int i = 0; i < 4; ++i) if (r[i]) g_roots.push_back(r[i]);
    return !g_roots.empty();
}

// Returns every distinct mob id found, sorted numerically.
static std::vector<std::string> list_mob_ids() {
    std::set<std::string> ids;
    for (nl::node root : g_roots) {
        for (nl::node c : root) {
            std::string n = c.name();
            if (n.size() > 4 && n.compare(n.size() - 4, 4, ".img") == 0) {
                std::string id = n.substr(0, n.size() - 4);
                if (is_uint(id)) ids.insert(id);
            }
        }
    }
    std::vector<std::string> out(ids.begin(), ids.end());
    std::sort(out.begin(), out.end(), [](const std::string & a, const std::string & b) {
        if (a.size() != b.size()) return a.size() < b.size();
        return a < b;
    });
    return out;
}

// Mob.img may live in any volume; Mob.nx first, then the numbered ones.
static nl::node find_mob(const std::string & id) {
    std::string img = id + ".img";
    for (nl::node root : g_roots) {
        nl::node n = root[img.c_str()];
        if (n) return n;
    }
    return nl::node();
}

static std::string mob_name(const std::string & id) {
    nl::node n = nl::nx::String["Mob.img"][id.c_str()]["name"];
    if (!n) return std::string();
    return n.get_string();
}

// Collects the bitmap nodes that make up one frame: either the frame node is
// the bitmap itself, or its direct children are the parts (0,1,2 / body,head..).
static void frame_sources(nl::node fr, std::vector<nl::node> & out) {
    if (fr.data_type() == nl::node::type::bitmap) { out.push_back(fr); return; }
    std::vector<std::pair<int, std::string>> numbered;  // numeric names first, in order
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

struct Src {
    int w = 0, h = 0, ox = 0, oy = 0;
    // Own copy of the decoded pixels. nl::bitmap::data() decodes into one
    // shared buffer, so with multi-part frames every part would alias the
    // same memory and each get_bitmap() call would clobber the previous part.
    // Never cache a raw pointer into this buffer: copying a Src gives `own`
    // its own storage, so a cached pointer would dangle after the copy.
    std::vector<unsigned char> own;
    const unsigned char * pixels() const { return own.data(); }
};

static bool decode(nl::node n, Src & s) {
    nl::bitmap b = n.get_bitmap();
    if (!b) return false;
    s.w = static_cast<int>(b.width());
    s.h = static_cast<int>(b.height());
    if (!s.w || !s.h) return false;
    // Decode manually with LZ4_decompress_safe. nl::bitmap::data() uses
    // LZ4_decompress_fast, which trusts the stream; one corrupt entry can
    // write past the buffer and wreck the heap much later.
    const char * comp = reinterpret_cast<const char *>(b.id());
    if (!comp) return false;
    uint32_t compLen = 0;
    std::memcpy(&compLen, comp, sizeof(compLen));
    if (compLen == 0 || compLen > (64u << 20)) return false;  // sanity guard
    size_t outLen = static_cast<size_t>(s.w) * s.h * 4;
    s.own.resize(outLen);
    int r = LZ4_decompress_safe(comp + 4, reinterpret_cast<char *>(s.own.data()),
                                static_cast<int>(compLen), static_cast<int>(outLen));
    if (r < 0) return false;
    nl::node o = n["origin"];
    if (o && o.data_type() == nl::node::type::vector) { s.ox = o.x(); s.oy = o.y(); }
    return true;
}

// Merges every part onto one canvas, honouring each part's origin. Returns the
// canvas plus the anchor offset (origin of the merged sprite) for reference.
static bool compose(const std::vector<Src> & srcs, std::vector<unsigned char> & canvas,
                    int & W, int & H, int & ax, int & ay) {
    if (srcs.empty()) return false;
    int minx = INT_MAX, miny = INT_MAX, maxx = INT_MIN, maxy = INT_MIN;
    for (const Src & s : srcs) {
        int lx = -s.ox, ly = -s.oy;            // sprite top-left relative to anchor
        minx = std::min(minx, lx);
        miny = std::min(miny, ly);
        maxx = std::max(maxx, lx + s.w);
        maxy = std::max(maxy, ly + s.h);
    }
    W = maxx - minx; H = maxy - miny;
    if (W <= 0 || H <= 0 || W > 8192 || H > 8192) return false;
    ax = -minx; ay = -miny;                    // anchor position inside the canvas
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
                    d[0] = static_cast<unsigned char>((p[2] * ia + d[0] * inv) / 255);
                    d[1] = static_cast<unsigned char>((p[1] * ia + d[1] * inv) / 255);
                    d[2] = static_cast<unsigned char>((p[0] * ia + d[2] * inv) / 255);
                    d[3] = static_cast<unsigned char>(ia + (d[3] * inv) / 255);
                }
            }
        }
    }
    return true;
}

static bool write_bgra_png(const std::string & path, const Src & s) {
    const unsigned char * src = s.pixels();
    std::vector<unsigned char> rgba(static_cast<size_t>(s.w) * s.h * 4);
    for (size_t i = 0; i < static_cast<size_t>(s.w) * s.h; ++i) {
        rgba[i * 4 + 0] = src[i * 4 + 2];
        rgba[i * 4 + 1] = src[i * 4 + 1];
        rgba[i * 4 + 2] = src[i * 4 + 0];
        rgba[i * 4 + 3] = src[i * 4 + 3];
    }
    return stbi_write_png(path.c_str(), s.w, s.h, 4, rgba.data(), s.w * 4) != 0;
}

static std::string norm_id(const std::string & s) {
    size_t p = s.find_first_not_of('0');
    if (p == std::string::npos) return "0";
    return s.substr(p);
}

static std::string safe_name(const std::string & s) {
    std::string out;
    for (unsigned char c : s) {
        if (c < 0x20 || c == '/' || c == '\\' || c == ':' || c == '*' || c == '?' ||
            c == '"' || c == '<' || c == '>' || c == '|') out += '_';
        else out += static_cast<char>(c);
    }
    while (!out.empty() && (out.back() == ' ' || out.back() == '.')) out.pop_back();
    return out;
}

// --------------------------------------------------------------------- survey

// 取 info 里的整数字段；缺失返回 -1（0 是合法值，不能用 0 当"没有"）。
static long long info_int(nl::node info, const char * key) {
    nl::node n = info[key];
    if (!n) return -1;
    if (n.data_type() == nl::node::type::integer) return n.get_integer();
    return -1;
}

// info/link 里的怪 id（字符串或整数），用来把 boss/等级等属性解析到真正那只怪。
static std::string info_link(nl::node info) {
    nl::node n = info["link"];
    if (!n) return std::string();
    if (n.data_type() == nl::node::type::string) return n.get_string();
    if (n.data_type() == nl::node::type::integer)
        return std::to_string(n.get_integer());
    return std::string();
}

// 导出图鉴属性：id,name,boss,level,maxhp,exp
// boss=1 是 WZ 官方标记（客户端画血条/播 boss 音乐就看它），比按 id 段或名字猜靠谱。
static int cmd_info() {
    std::vector<std::string> ids = list_mob_ids();
    std::cout << "id,name,boss,level,maxhp,exp\n";
    for (const std::string & id : ids) {
        nl::node mob = find_mob(id);
        if (!mob) continue;
        nl::node info = mob["info"];
        if (!info) continue;
        // link 怪自己没有完整 info，往目标跳（最多 4 跳，防环）
        nl::node src = info;
        std::string target = info_link(info);
        for (int hop = 0; hop < 4 && !target.empty(); ++hop) {
            nl::node t = find_mob(norm_id(target));
            if (!t || !t["info"]) break;
            src = t["info"];
            target = info_link(src);
        }
        long long boss = info_int(src, "boss");
        long long lv = info_int(src, "level");
        long long hp = info_int(src, "maxHP");
        long long exp = info_int(src, "exp");
        std::string nm = mob_name(id);
        std::cout << id << ",\"" << nm << "\"," << (boss > 0 ? 1 : 0)
                  << "," << lv << "," << hp << "," << exp << "\n";
    }
    std::cout.flush();
    return 0;
}

static int cmd_count() {
    std::vector<std::string> ids = list_mob_ids();
    long long anims = 0, frames = 0, parts = 0, linked = 0, empty = 0;
    int maxf = 0;
    std::string maxid;
    for (const std::string & id : ids) {
        nl::node mob = find_mob(id);
        if (!mob) continue;
        nl::node info = mob["info"];
        if (info && info["link"]) { ++linked; continue; }
        int mobFrames = 0;
        for (nl::node anim : mob) {
            std::string an = anim.name();
            if (an == "info" || an == "link") continue;
            if (!anim["0"]) continue;
            ++anims;
            for (int f = 0;; ++f) {
                nl::node fr = anim[std::to_string(f).c_str()];
                if (!fr) break;
                ++frames; ++mobFrames;
                std::vector<nl::node> srcs;
                frame_sources(fr, srcs);
                parts += static_cast<long long>(srcs.size());
            }
        }
        if (mobFrames == 0) ++empty;
        if (mobFrames > maxf) { maxf = mobFrames; maxid = id; }
    }
    std::cout << "mobs=" << ids.size() << " linked=" << linked << " empty=" << empty << "\n";
    std::cout << "anims=" << anims << " frames=" << frames << " parts=" << parts << "\n";
    std::cout << "max_frames mob=" << maxid << " (" << maxf << ")\n";
    return 0;
}

// --------------------------------------------------------------------- export

static std::string g_curMob;   // crash forensics: mob being exported
static std::string g_curPart;  // crash forensics: anim/frame being written

static LONG WINAPI on_crash(EXCEPTION_POINTERS * ep) {
    const EXCEPTION_RECORD & r = *ep->ExceptionRecord;
    void * base = static_cast<void *>(GetModuleHandle(nullptr));
    std::fprintf(stderr, "\nCRASH code=0x%08lX addr=%p", r.ExceptionCode,
                 r.ExceptionAddress);
    if (base) {
        auto ba = reinterpret_cast<const unsigned char *>(base);
        auto ea = reinterpret_cast<const unsigned char *>(r.ExceptionAddress);
        if (ea >= ba && ea < ba + (1u << 30))
            std::fprintf(stderr, " (+0x%X)", static_cast<unsigned>(ea - ba));
    }
    if (r.ExceptionCode == EXCEPTION_ACCESS_VIOLATION && r.NumberParameters >= 2) {
        void * bad = reinterpret_cast<void *>(r.ExceptionInformation[1]);
        MEMORY_BASIC_INFORMATION mi{};
        std::fprintf(stderr, " %s addr=%p",
                     r.ExceptionInformation[0] ? "write" : "read", bad);
        if (VirtualQuery(bad, &mi, sizeof(mi)))
            std::fprintf(stderr, " [state=0x%X type=0x%X protect=0x%X base=%p size=%llu]",
                         mi.State, mi.Type, mi.Protect, mi.AllocationBase,
                         static_cast<unsigned long long>(mi.RegionSize));
    }
    std::fprintf(stderr, " mob=%s part=%s\n", g_curMob.c_str(), g_curPart.c_str());
    std::fflush(stderr);
    return EXCEPTION_EXECUTE_HANDLER;
}

struct Opts {
    bool stand_only = false;
    bool first_frame = false;
    bool flat = false;
    bool composite = true;
    bool named = false;
    std::string only;          // export a single mob id
};

static int cmd_export(const std::string & outdir, const Opts & opt) {
    std::vector<std::string> ids = list_mob_ids();
    if (!opt.only.empty()) ids = { opt.only };
    fs::create_directories(outdir);
    std::ofstream man(fs::path(outdir) / "manifest.csv");
    // ox/oy: the frame's anchor inside the png (= distance from png left/top to
    // the sprite's origin). Without it the frames of one animation cannot be
    // lined up: every WZ canvas is trimmed to its own bbox, so a 28x28 frame
    // and a 31x28 frame of the same pose would otherwise drift apart.
    // delay: ms this frame is held on screen (0 when the WZ omits it).
    man << "\xEF\xBB\xBF" << "id,name,anim,frame,file,w,h,ox,oy,delay\n";

    long long written = 0, mobs_done = 0, mobs_skip = 0;
    long long lastMark = -1;
    std::vector<std::string> madeDirs;   // cleaned up at the end if left empty
    for (const std::string & id : ids) {
        g_curMob = id;
        nl::node mob = find_mob(id);
        if (!mob) { ++mobs_skip; continue; }
        g_curPart.clear();
        // Some mob names carry characters Windows rejects; a bad folder name
        // must not abort the whole run, so fall back to the plain id.
        std::string name, norm, dirname, dir;
        try {
            name = mob_name(id);
            norm = norm_id(id);
            dirname = opt.named && !name.empty() ? (norm + "_" + safe_name(name)) : norm;
            dir = opt.flat ? outdir : (fs::path(outdir) / dirname).string();
        }
        catch (const std::exception &) {
            // Retry with the bare id - it is the mob name that breaks the path.
            try {
                dirname = norm_id(id);
                dir = opt.flat ? outdir : (fs::path(outdir) / dirname).string();
            }
            catch (const std::exception & e2) {
                std::fprintf(stderr, "SKIP mob=%s (name/dir): %s\n", id.c_str(), e2.what());
                std::fflush(stderr);
                ++mobs_skip;
                continue;
            }
        }

        std::error_code ecDir;
        bool dirReady = opt.flat;            // create the folder lazily, on first png
        auto ensure_dir = [&]() -> bool {
            if (dirReady) return true;
            try {
                if (fs::create_directories(dir, ecDir) || fs::is_directory(dir, ecDir)) {
                    dirReady = true;
                    madeDirs.push_back(dir);
                    return true;
                }
            }
            catch (const std::exception & e) {
                std::fprintf(stderr, "SKIP dir mob=%s: %s\n", id.c_str(), e.what());
                std::fflush(stderr);
            }
            return false;
        };

        int mobWritten = 0;
        for (nl::node anim : mob) {
            std::string an = anim.name();
            if (an == "info" || an == "link") continue;
            if (!anim["0"]) continue;
            if (opt.stand_only && an != "stand" && an != "stand1" && an != "stand2") continue;
            std::string asafe = safe_name(an);
            for (int f = 0;; ++f) {
                nl::node fr = anim[std::to_string(f).c_str()];
                if (!fr) break;
                std::vector<nl::node> srcnodes;
                try {
                frame_sources(fr, srcnodes);
                std::vector<Src> srcs;
                for (nl::node n : srcnodes) { Src s; if (decode(n, s)) srcs.push_back(s); }
                if (srcs.empty()) continue;

                char fbuf[16];
                std::snprintf(fbuf, sizeof(fbuf), "%02d", f);
                g_curPart = an + "/" + std::to_string(f);
                // Per-frame hold time lives on the frame node (anim/f/delay).
                int delay = 0;
                nl::node dl = fr["delay"];
                if (dl && dl.data_type() == nl::node::type::integer)
                    delay = static_cast<int>(dl.get_integer());
                std::string base = opt.flat ? (dirname + "_" + asafe + "_" + fbuf)
                                            : (asafe + "_" + fbuf);
                fs::path out = fs::path(dir) / (base + ".png");

                if (opt.composite) {
                    std::vector<unsigned char> canvas;
                    int W = 0, H = 0, ax = 0, ay = 0;
                    if (!compose(srcs, canvas, W, H, ax, ay)) continue;
                    if (!ensure_dir()) continue;
                    if (stbi_write_png(out.string().c_str(), W, H, 4, canvas.data(), W * 4) == 0)
                        continue;
                    man << norm << ",\"" << name << "\"," << an << "," << f << ",\""
                        << out.filename().string() << "\"," << W << "," << H << ","
                        << ax << "," << ay << "," << delay << "\n";
                    ++written; ++mobWritten;
                } else {
                    int pi = 0;
                    for (const Src & s : srcs) {
                        char pbuf[24];
                        std::snprintf(pbuf, sizeof(pbuf), "_p%d", pi++);
                        if (!ensure_dir()) break;
                        fs::path p = fs::path(dir) / (base + pbuf + ".png");
                        if (!write_bgra_png(p.string(), s)) continue;
                        man << norm << ",\"" << name << "\"," << an << "," << f << ",\""
                            << p.filename().string() << "\"," << s.w << "," << s.h << ","
                            << s.ox << "," << s.oy << "," << delay << "\n";
                        ++written; ++mobWritten;
                    }
                }
                }
                catch (const std::exception & e) {
                    std::fprintf(stderr, "SKIP mob=%s part=%s: %s\n",
                                 id.c_str(), g_curPart.c_str(), e.what());
                    std::fflush(stderr);
                }
                if (opt.first_frame) break;
            }
        }
        if (mobWritten) ++mobs_done; else ++mobs_skip;
        std::printf("mob %s ok (%d png, total %lld)\n", id.c_str(), mobWritten, written);
        std::fflush(stdout);
        if (written / 500 != lastMark) {
            lastMark = written / 500;
            PROCESS_MEMORY_COUNTERS pmc{};
            size_t ws = 0;
            if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc)))
                ws = static_cast<size_t>(pmc.WorkingSetSize);
            std::printf("progress: %lld png, mob %s, ws=%.0fMB\n",
                        written, id.c_str(), ws / 1048576.0);
            std::fflush(stdout);
        }
    }
    man.close();
    // Drop folders this run created but never wrote to (mostly info.link mobs),
    // plus any empty "<id>" folder left over from an earlier run. Only folders
    // whose name starts with a digit are touched - never anything else in outdir.
    std::error_code ec;
    for (const std::string & d : madeDirs)
        if (fs::is_directory(d, ec) && fs::is_empty(d, ec)) fs::remove(d, ec);
    if (!opt.flat) {
        for (const auto & e : fs::directory_iterator(outdir, ec)) {
            if (!e.is_directory()) continue;
            std::string n = e.path().filename().string();
            if (n.empty() || !std::isdigit(static_cast<unsigned char>(n[0]))) continue;
            std::error_code ec2;
            if (fs::is_empty(e.path(), ec2)) fs::remove(e.path(), ec2);
        }
    }
    std::cout << "mobs_exported=" << mobs_done << " mobs_skipped=" << mobs_skip
              << " png=" << written << "\noutdir=" << outdir << "\n";
    return 0;
}

int main(int argc, char ** argv) {
    if (argc < 2) {
        std::cerr << "usage:\n"
                     "  nxmob count\n"
                     "  nxmob info                        -- csv: id,name,boss,level,maxhp,exp\n"
                     "  nxmob export <outdir> [--stand-only] [--first-frame] [--flat]\n"
                     "                        [--no-composite] [--named]\n";
        return 1;
    }
    nl::nx::load_all();
    SetUnhandledExceptionFilter(on_crash);
    if (!collect_roots()) { std::cerr << "Mob.nx not found in cwd\n"; return 2; }

    std::string cmd = argv[1];
    if (cmd == "count") return cmd_count();
    if (cmd == "info") return cmd_info();
    if (cmd == "export") {
        if (argc < 3) { std::cerr << "missing outdir\n"; return 1; }
        Opts o;
        for (int i = 3; i < argc; ++i) {
            std::string a = argv[i];
            if (a == "--stand-only") o.stand_only = true;
            else if (a == "--first-frame") o.first_frame = true;
            else if (a == "--flat") o.flat = true;
            else if (a == "--no-composite") o.composite = false;
            else if (a == "--named") o.named = true;
            else if (a == "--only" && i + 1 < argc) o.only = argv[++i];
            else { std::cerr << "unknown option " << a << "\n"; return 1; }
        }
        return cmd_export(argv[2], o);
    }
    std::cerr << "unknown command " << cmd << "\n";
    return 1;
}
