# Beyond Tournament

Beyond Tournament is an open-source, audio-based game inspired by the Zombies mode in the Call of Duty series. Players can team up online to fight off hordes of zombies, aiming for high scores, kill counts, and an enjoyable experience.

## ✨ Features & Recent Improvements

*   **Online Co-op Gameplay:** Play with friends and other players online to survive against the undead.
*   **Immersive 3D Audio Experience:** Designed entirely as an audio game, Beyond Tournament provides a rich, directional soundscape for players, complete with footstep tracking, proximity voice chat, and radar beeps.
*   **Refined Spectator & Ghost Systems:** Watch matches without interfering. The spectator mode has been overhauled with dedicated keybinds for chat scrolling and channel switching, while ensuring your physical presence remains completely undetected by zombies and other players' radars.
*   **Inspired by Call of Duty Zombies:** Experience gameplay mechanics, weapons, and items reminiscent of the Aether timeline in the Call of Duty Zombies series.
*   **Cross-Platform:** While primarily developed on Windows, the game is built with cross-platform compatibility in mind.

## Getting Started

These instructions will get you a copy of the project up and running on your local machine for development and testing purposes.

### Prerequisites

On 64-bit Windows, the automatic setup handles Python and all Python
libraries. Visual Studio and the Desktop development with C++ workload are not
required. Python 3.12 is preferred and Python 3.11 is also supported. Newer
system Python installations are left untouched when a dependency does not yet
provide a compatible wheel.

### Installation

1.  Clone the repository to your local machine:

    ```sh
    git clone https://github.com/qiwnewlove2014-cmd/Beyond-Tournament-Client.git
    ```

2.  Navigate to the project directory:

    ```sh
    cd Beyond-Tournament-Client
    ```

3.  Run the accessible console setup:

    ```bat
    install_libs.bat
    ```

    The setup reports every action, creates an isolated `.venv`, requires
    prebuilt wheels for native libraries, and performs a complete import and
    project-file health check. If no compatible Python is installed, it uses
    Windows Package Manager to install Python 3.12 for the current user. During
    the first compilation, Nuitka may download its portable MinGW64 compiler;
    Visual Studio Build Tools are not needed.

    Useful diagnostics:

    ```bat
    install_libs.bat -DryRun
    install_libs.bat -SelfTest
    build.bat --check
    ```

### Running the Game

To play the game, run the following command in the project's root directory on a Windows machine:

```sh
launch.bat
```

## Building the Game

To build an executable version of the game, follow these steps:

1.  **Update the Version:** Before building, edit `libs/version.py` to set a new version number, following semantic versioning (e.g., `major.minor.patch`).

2.  **Commit the Version Change:** Commit the updated `libs/version.py` file to your local Git repository.

3.  **Tag the Release:** Create a new Git tag for the release:

    ```sh
    git tag -a X.Y.Z -m "Release X.Y.Z"
    ```

    Replace `X.Y.Z` with the version number you set in `libs/version.py`.

4.  **Run the Build Script:** Execute the build script to create the game executable:

    ```sh
    build.bat
    ```

## Contributing

Contributions are welcome! If you'd like to contribute to Beyond Tournament, please follow these steps:

1.  Fork the repository.
2.  Create a new branch for your feature or bug fix.
3.  Make your changes and commit them with clear, descriptive messages, following convencional commits.
4.  Push your changes to your forked repository.
5.  Create a pull request to the main repository's `main` branch.

Please ensure your code adheres to the project's existing code style and conventions.

## License

This project is licensed under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file for more details.

## Acknowledgments

*   This project is heavily inspired by the Zombies mode in the Call of Duty series, particularly the Aether storyline.
*   We utilize resources from the Call of Duty fan community for weapon stats, character quotes, and gameplay mechanics.
*   A special thanks to the creators of the original Call of Duty Zombies for their incredible work.

