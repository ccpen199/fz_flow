from app.mock_data import ensure_demo_database


def main() -> None:
    db_path, created = ensure_demo_database()
    if created:
        print(f"[bootstrap] Created demo database at: {db_path}")
    else:
        print(f"[bootstrap] Demo database already exists: {db_path}")


if __name__ == "__main__":
    main()
