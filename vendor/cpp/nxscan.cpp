// nxscan: dump the subtree structure of an NX file (names, types, sizes, delays).
//   nxscan <NxFile> <subpath> [maxdepth]
// e.g.  nxscan Effect.nx "BasicEff.img" 4
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <nlnx/nx.hpp>
#include <nlnx/node.hpp>
#include <nlnx/bitmap.hpp>

#include <iostream>
#include <string>

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
    // load_all resolves file names like "Effect" -> Effect.nx
    nl::nx::load_all();
    nl::node root;
    if (std::string(argv[1]) == "Effect") root = nl::nx::Effect;
    else if (std::string(argv[1]) == "Character") root = nl::nx::Character;
    else if (std::string(argv[1]) == "Sound") root = nl::nx::Sound;
    else if (std::string(argv[1]) == "UI") root = nl::nx::UI;
    else if (std::string(argv[1]) == "Mob") root = nl::nx::Mob;
    else if (std::string(argv[1]) == "Map") root = nl::nx::Map;
    else if (std::string(argv[1]) == "Item") root = nl::nx::Item;
    else if (std::string(argv[1]) == "String") root = nl::nx::String;
    else if (std::string(argv[1]) == "Npc") root = nl::nx::Npc;
    else { std::cerr << "unknown file alias\n"; return 1; }

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
