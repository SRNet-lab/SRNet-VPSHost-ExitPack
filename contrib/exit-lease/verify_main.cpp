// exit-lease-verify STORE ISSUER_PUBKEY_HEX SELLER_SESH CLIENT_SESH
// Prints the reason the exit would give; exit 0 only on "admission".
// Seller-pack sanity tool and cross-language test hook.
#include "auth/exit_lease_admission.hpp"

#include <chrono>
#include <cstdio>
#include <sodium/core.h>

int main(int argc, char** argv)
{
    if (argc != 5)
    {
        std::fprintf(stderr, "usage: %s STORE ISSUER_PUBKEY_HEX SELLER_SESH CLIENT_SESH\n", argv[0]);
        return 2;
    }
    if (sodium_init() < 0)
        return 3;
    const auto now = std::chrono::duration_cast<std::chrono::seconds>(
                         std::chrono::system_clock::now().time_since_epoch())
                         .count();
    const auto r = srouter::auth::exit_lease_auth_allows(false, false, false, argv[1], argv[2], argv[3], argv[4], now);
    std::puts(r.reason);
    return r.allow ? 0 : 1;
}
