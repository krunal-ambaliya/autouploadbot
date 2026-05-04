from tmdb_service import search_tmdb


def get_movie_or_series(name):
    details = search_tmdb(name)

    if not details:
        print("No results found.")
        return

    print("\n--- Result ---")
    print("Title:", details["title"])
    print("Year:", details["year"])
    print("Poster:", details["poster_url"])
    print("Description:", details["description"])


if __name__ == "__main__":
    name = input("Enter movie/series name: ")
    get_movie_or_series(name)