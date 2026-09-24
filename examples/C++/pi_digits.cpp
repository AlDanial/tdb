// Build: c++ -g -O0 -std=c++11 -pthread pi_digits.cpp -o pi_digits_cpp
// Run:   ./pi_digits_cpp -r 2 -d 20 -t 2 -p
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <iomanip>

using Clock = std::chrono::steady_clock;
struct Result { std::string digits; double seconds; };

std::string pi_digits(int count) {
    std::vector<int> a(count * 10 / 3 + 1, 2);
    std::string out;
    int previous = 0, pending = 0;
    for (int step = 0; step <= count; ++step) {
        long long q = 0;
        for (int i = static_cast<int>(a.size()); i >= 1; --i) {
            long long x = 10LL * a[i - 1] + q * i;
            a[i - 1] = static_cast<int>(x % (2 * i - 1));
            q = x / (2 * i - 1);
        }
        a[0] = static_cast<int>(q % 10);
        q /= 10;
        if (q == 9) ++pending;
        else if (q == 10) {
            out += char('0' + previous + 1);
            out.append(pending, '0');
            previous = pending = 0;
        } else {
            out += char('0' + previous);
            out.append(pending, '9');
            pending = 0; previous = static_cast<int>(q);
        }
    }
    out += char('0' + previous);
    out.append(pending, '9');
    return out.substr(1, count);
}

int main(int argc, char **argv) {
    int runs = 1, count = 10, threads = 1;
    bool print = false;
    try {
        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i];
            if (arg == "-p" || arg == "--print") print = true;
            else if (i + 1 < argc && (arg == "-r" || arg == "--n-runs")) runs = std::stoi(argv[++i]);
            else if (i + 1 < argc && (arg == "-d" || arg == "--n-digits")) count = std::stoi(argv[++i]);
            else if (i + 1 < argc && (arg == "-t" || arg == "--n-threads")) threads = std::stoi(argv[++i]);
            else throw std::invalid_argument("unknown or incomplete option: " + arg);
        }
        if (runs < 1 || count < 1 || threads < 1) throw std::invalid_argument("counts must be positive");
        for (int run = 1; run <= runs; ++run) {
            auto run_start = Clock::now();
            std::vector<Result> results(threads);
            std::vector<std::thread> workers;
            for (int j = 0; j < threads; ++j) workers.emplace_back([&, j] {
                auto start = Clock::now();
                results[j].digits = pi_digits(count);
                results[j].seconds = std::chrono::duration<double>(Clock::now() - start).count();
            });
            for (int j = 0; j < threads; ++j) {
                workers[j].join();
                std::cout << std::fixed << std::setprecision(6);
                if (threads > 1) std::cout << "Run " << std::setw(3) << run << '/' << std::setw(3) << runs << ", thread " << std::setw(3) << j+1 << '/' << std::setw(3) << threads;
                else std::cout << "Run " << std::setw(3) << run << '/' << std::setw(3) << runs;
                std::cout << " completed in " << results[j].seconds << " seconds.\n";
            }
            if (threads > 1) std::cout << "== Run " << std::setw(3) << run << '/' << std::setw(3) << runs << " completed in " << std::chrono::duration<double>(Clock::now() - run_start).count() << " seconds.\n";
            if (run == runs && print) std::cout << results[0].digits << '\n';
        }
    } catch (const std::exception &e) { std::cerr << e.what() << '\n'; return 2; }
}
