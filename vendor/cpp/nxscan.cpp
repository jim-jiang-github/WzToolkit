// nxscan: dump the subtree structure of an NX file (names, types, sizes, delays).
//   nxscan <NxFile> <subpath> [maxdepth]
// e.g.  nxscan Effect.nx "BasicEff.img" 4
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <nlnx/nx.hpp>
#include <nlnx/node.hpp>
#include <nlnx/bitmap.hpp>

#include <iostream>
#include <string>

// nlnx only declares load_all(); the CN trial layout ships a single
// self-contained Map.nx / String.nx, so open files by name directly.
namespace nl { namespace nx { node add_file(std::string name); } }

static void dump(nl::node n, const std::string & path, int depth, int maxdepth) {
    if (depth > maxdepth || !n) return;
    for (nl::node child : n) {
        std::string p = path + "/" + child.name();
        const char * t = "?";
        switch (child.data_type()) {
            case nl::node::type::bitmap: t = "bmp"; break;
            case nl::node::type::integer: t = "int"; break;
            case nl::node::type::real: t = "real"; break;
            case nl::node::type::string: t = "str"; break;
            case nl::node::type::vector: t = "vec"; break;
            case nl::node::type::audio: t = "aud"; break;
            default: t = "none"; break;
        }
        std::cout << p << " [" << t << "]";
        if (child.data_type() == nl::node::type::bitmap) {
            nl::bitmap b = child.get_bitmap();
            if (b) std::cout << " " << b.width() << "x" << b.height();
        } else if (child.data_type() == nl::node::type::integer) {
            std::cout << " = " << child.get_integer();
        } else if (child.data_type() == nl::node::type::string) {
            std::string s = child.get_string();
            if (s.size() < 60) std::cout << " = " << s;
        }
        std::cout << "\n";
        dump(child, p, depth + 1, maxdepth);
    }
}

int main(int argc, char ** argv) {
    if (argc < 3) { std::cerr << "usage: nxscan <NxFile> <subpath> [maxdepth]\n"; return 1; }
    std::string alias(argv[1]);
    nl::node root;
    // Any layout: try opening <alias>.nx next to the exe first (CN trial layout
    // ships self-contained X.nx files, everything under one folder named like
    // the file). Falls back to the classic set below.
    {
        nl::node f = nl::nx::add_file(alias + ".nx");
        if (f) root = f[alias.c_str()];
    }
    if (!root) {
        // Fall back to the classic set (load_all resolves "Effect" -> Effect.nx).
        nl::nx::load_all();
        if (alias == "Effect") root = nl::nx::Effect;
        else if (alias == "Character") root = nl::nx::Character;
        else if (alias == "Sound") root = nl::nx::Sound;
        else if (alias == "UI") root = nl::nx::UI;
        else if (alias == "Mob") root = nl::nx::Mob;
        else if (alias == "Map") root = nl::nx::Map;
        else if (alias == "Item") root = nl::nx::Item;
        else if (alias == "String") root = nl::nx::String;
        else if (alias == "Npc") root = nl::nx::Npc;
        else { std::cerr << "unknown file alias\n"; return 1; }
    }

    std::string p(argv[2]);
    size_t start = 0;
    while (start < p.size()) {
        size_t slash = p.find('/', start);
        std::string seg = p.substr(start, slash == std::string::npos ? std::string::npos : slash - start);
        if (!seg.empty()) root = root[seg];
        if (slash == std::string::npos) break;
        start = slash + 1;
    }
    if (!root) { std::cerr << "path not found\n"; return 2; }
    int maxdepth = argc > 3 ? std::stoi(argv[3]) : 4;
    dump(root, std::string(argv[1]) + "/" + p, 0, maxdepth);
    return 0;
}
