"""
Test bracket seeding logic for play-in games and Round 1 pairing.

This file tests the bracket creation algorithm to ensure proper seeding.
"""


def test_bracket_seeding(num_players):
    """
    Test bracket seeding for a given number of players.

    Expected behavior:
    - Top seeds get byes
    - Bottom seeds play in play-in games
    - Play-in winners should pair with bye seeds such that:
      * Best bye seed plays winner of WORST play-in matchup
      * 2nd best bye seed plays winner of 2nd WORST play-in matchup
      * etc.
    """
    print(f"\n{'=' * 60}")
    print(f"Testing bracket with {num_players} players")
    print(f"{'=' * 60}")

    seeds = list(range(1, num_players + 1))

    # Check if power of 2
    if num_players & (num_players - 1) == 0:
        print("✓ Power of 2 - no play-ins needed")
        print("First round pairings:")
        pair_count = num_players // 2
        for slot in range(pair_count):
            p1 = seeds[slot]
            p2 = seeds[-(slot + 1)]
            print(f"  Match {slot + 1}: {p1} vs {p2}")
        return

    # Need play-in games
    target_bracket_size = 1 << (num_players.bit_length() - 1)
    num_play_in_games = num_players - target_bracket_size
    num_byes = target_bracket_size - num_play_in_games

    print(f"Target bracket size: {target_bracket_size}")
    print(f"Play-in games needed: {num_play_in_games}")
    print(f"Byes: {num_byes}")

    bye_seeds = seeds[:num_byes]
    play_in_seeds = seeds[num_byes:]

    print(f"\nBye seeds (top {num_byes}): {bye_seeds}")
    print(f"Play-in seeds (bottom {len(play_in_seeds)}): {play_in_seeds}")

    # Show play-in games
    print("\n--- PLAY-IN ROUND ---")
    play_in_games = []
    for i in range(num_play_in_games):
        p1 = play_in_seeds[i]
        p2 = play_in_seeds[-(i + 1)]
        play_in_games.append((p1, p2))
        print(f"  Play-in Game {i}: {p1} vs {p2}")

    # Create bracket positions
    bracket_positions = [None] * target_bracket_size

    # Place bye seeds in TOP positions
    for i in range(num_byes):
        bracket_positions[i] = bye_seeds[i]

    # CURRENT LOGIC (IN REVERSE ORDER):
    print("\n--- TESTING CURRENT LOGIC (REVERSE ORDER) ---")
    bracket_positions_current = bracket_positions.copy()
    for i in range(num_play_in_games):
        bracket_positions_current[target_bracket_size - 1 - i] = (
            f"W({play_in_games[i][0]}v{play_in_games[i][1]})"
        )

    print(f"Bracket positions: {bracket_positions_current}")
    print("\nRound 1 matches (current logic):")
    main_bracket_first_round_matches = target_bracket_size // 2
    for i in range(main_bracket_first_round_matches):
        p1 = bracket_positions_current[i]
        p2 = bracket_positions_current[target_bracket_size - 1 - i]
        print(f"  Match {i + 1}: {p1} vs {p2}")

    # ALTERNATIVE LOGIC (IN ASCENDING ORDER):
    print("\n--- TESTING ALTERNATIVE LOGIC (ASCENDING ORDER) ---")
    bracket_positions_alt = bracket_positions.copy()
    for i in range(num_play_in_games):
        bracket_positions_alt[num_byes + i] = (
            f"W({play_in_games[i][0]}v{play_in_games[i][1]})"
        )

    print(f"Bracket positions: {bracket_positions_alt}")
    print("\nRound 1 matches (alternative logic):")
    for i in range(main_bracket_first_round_matches):
        p1 = bracket_positions_alt[i]
        p2 = bracket_positions_alt[target_bracket_size - 1 - i]
        print(f"  Match {i + 1}: {p1} vs {p2}")

    # EXPECTED BEHAVIOR
    print("\n--- EXPECTED BEHAVIOR ---")
    print("Based on tournament convention:")
    print("- Seed 1 (best) should play winner of WORST play-in game")
    print("- Seed 2 should play winner of 2nd WORST play-in game")
    print("- etc.")
    print(f"\nFor {num_players} players, expected Round 1:")

    # Expected: best seed plays worst play-in winner
    # Play-in games from current logic are ordered:
    # Game 0: play_in_seeds[0] vs play_in_seeds[-1] (e.g., 5 vs 12 for 12 players - middle seeds)
    # Game 1: play_in_seeds[1] vs play_in_seeds[-2] (e.g., 6 vs 11)
    # ...
    # The LAST game has the WORST seeds

    for i in range(main_bracket_first_round_matches):
        bye_seed = bye_seeds[i] if i < len(bye_seeds) else None
        if bye_seed:
            # Which play-in game winner should this bye seed face?
            # Best bye seed (1) should face winner of LAST (worst) play-in game
            play_in_game_index = num_play_in_games - 1 - i
            if play_in_game_index >= 0:
                game = play_in_games[play_in_game_index]
                print(f"  Match {i + 1}: {bye_seed} vs W({game[0]}v{game[1]})")


if __name__ == "__main__":
    # Test various player counts
    test_cases = [6, 9, 12, 19, 31]

    for num_players in test_cases:
        test_bracket_seeding(num_players)

    print(f"\n{'=' * 60}")
    print("KEY QUESTION:")
    print("Which logic produces the expected behavior?")
    print("- Current (reverse order)")
    print("- Alternative (ascending order)")
    print("- Neither (need different approach)")
    print(f"{'=' * 60}\n")
