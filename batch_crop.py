def batch_crop(input_dir, output_dir):
    """
    Crop all PDFs in the input directory and save them to the output directory.

    Parameters:
    - input_dir: str, path to the directory containing input PDFs
    - output_dir: str, path to the directory where cropped PDFs will be saved
    """
    import os
    import subprocess

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Iterate through all files in the input directory
    for filename in os.listdir(input_dir):
        if filename.lower().endswith('.pdf'):
            input_path = os.path.join(input_dir, filename)
            # Create the output path for the cropped PDF, strip unused parts of the filename
            new_filename = filename.split(
                ' (Z-Lib')[0].split(
                ' (z-lib')[0].split(
                '-cdeKey_')[0].strip('.pdf') + '.pdf'
            output_path = os.path.join(output_dir, new_filename)

            # crop the pdf
            subprocess.run(['kindle-crop.exe', '--split',
                           '-o', output_path, input_path])
            print(f"Cropped and saved: {output_path}")


if __name__ == "__main__":
    import argparse

    # Set up argument parser
    parser = argparse.ArgumentParser(
        description="Batch crop PDFs in a directory.")
    parser.add_argument("input_dir", type=str,
                        help="Path to the input directory containing PDFs.")
    parser.add_argument("output_dir", type=str,
                        help="Path to the output directory for cropped PDFs.")

    # Parse arguments
    args = parser.parse_args()

    # Call the batch_crop function with parsed arguments
    batch_crop(args.input_dir, args.output_dir)
